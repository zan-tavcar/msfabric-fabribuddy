"""Orchestration notebooks: runMultiple DAGs and notebook.run arguments. Synthetic fixtures."""

from fabribuddy_core.lineage.parsers.notebook import parse_notebook


def _notebook(*cells: str) -> str:
    body = "\n\n".join(f"# CELL ********************\n\n{c.strip()}\n" for c in cells)
    meta = '# META {"dependencies": {"lakehouse": {"default_lakehouse_name": "lh"}}}'
    return f"# Fabric notebook source\n\n# METADATA ********************\n{meta}\n\n{body}"



CHILD = """# Fabric notebook source

# METADATA ********************
# META {"dependencies": {"lakehouse": {"default_lakehouse_name": "lh"}}}

# PARAMETERS CELL ********************

table = "default_table"
key = "Id"

# CELL ********************

spark.read.table("staging_" + table).write.mode("overwrite").saveAsTable(table)
"""

INLINE_DAG = _notebook(
    """
DAG = {
    "activities": [
        {"name": "merge_sales", "path": "nb_merge",
         "args": {"table": "sales", "key": "SaleId"}},
        {"name": "merge_orders", "path": "nb_merge",
         "args": {"table": "order", "key": "OrderId"},
         "dependencies": ["merge_sales"]},
    ]
}
notebookutils.notebook.runMultiple(DAG)
"""
)


def test_inline_dag_invokes_each_activity_with_its_args():
    result = parse_notebook(INLINE_DAG)
    assert not result.warnings
    assert result.invokes == ["nb_merge", "nb_merge"]
    assert result.invoke_parameters == [
        ("nb_merge", {"table": "sales", "key": "SaleId"}),
        ("nb_merge", {"table": "order", "key": "OrderId"}),
    ]


def test_dag_passed_inline_to_the_call():
    result = parse_notebook(
        _notebook(
            'mssparkutils.notebook.runMultiple({"activities": '
            '[{"name": "a", "path": "/Shared/nb_a", "args": {"day": 1}}]})'
        )
    )
    assert result.invoke_parameters == [("/Shared/nb_a", {"day": "1"})]


def test_dag_assigned_in_an_earlier_cell_with_values_from_variables():
    result = parse_notebook(
        _notebook(
            'env = "prod"\nchild = "nb_merge"',
            'DAG = {"activities": [{"name": "m", "path": child, '
            '"args": {"table": f"sales_{env}", "full": True}}]}',
            "notebookutils.notebook.runMultiple(DAG, {'displayDAGViaGraphviz': False})",
        )
    )
    assert not result.warnings
    assert result.invoke_parameters == [("nb_merge", {"table": "sales_prod", "full": "True"})]


def test_dag_from_a_run_config_notebook():
    config = _notebook('DAG = {"activities": [{"name": "m", "path": "nb_merge", "args": {"table": "sales"}}]}')
    result = parse_notebook(
        _notebook("%run nb_config", "notebookutils.notebook.runMultiple(DAG)"),
        run_sources={"nb_config": config},
    )
    assert ("nb_merge", {"table": "sales"}) in result.invoke_parameters


def test_list_form_invokes_each_notebook_without_parameters():
    result = parse_notebook(_notebook('notebookutils.notebook.runMultiple(["nb_a", "nb_b"])'))
    assert not result.warnings
    assert result.invokes == ["nb_a", "nb_b"]
    assert result.invoke_parameters == []


def test_notebook_run_with_an_arguments_dict():
    result = parse_notebook(
        _notebook(
            'notebookutils.notebook.run("nb_merge", 90, {"table": "sales"})',
            'args = {"table": "order"}\nmssparkutils.notebook.run("nb_merge", 90, arguments=args)',
        )
    )
    assert result.invokes == ["nb_merge", "nb_merge"]
    assert result.invoke_parameters == [
        ("nb_merge", {"table": "sales"}),
        ("nb_merge", {"table": "order"}),
    ]


def test_dynamic_dag_stays_unresolved():
    result = parse_notebook(
        _notebook(
            "rows = spark.read.table('meta.tables').collect()",
            'DAG = {"activities": [{"name": r.name, "path": "nb_merge", "args": {"table": r.name}} for r in rows]}',
            "notebookutils.notebook.runMultiple(DAG)",
        )
    )
    assert result.invoke_parameters == []
    assert any("dynamic runMultiple DAG" in w and "could not" in w for w in result.warnings)


def test_activity_with_unknown_args_is_reported_not_run_with_defaults():
    result = parse_notebook(
        _notebook(
            'DAG = {"activities": [{"name": "m", "path": "nb_merge", "args": {"table": unknown_name}}]}',
            "notebookutils.notebook.runMultiple(DAG)",
        )
    )
    assert result.invokes == ["nb_merge"]
    assert result.invoke_parameters == []
    assert any("dynamic runMultiple DAG" in w for w in result.warnings)


def test_each_args_set_reparses_the_child_into_its_own_write():
    orchestrator = parse_notebook(INLINE_DAG)
    written = set()
    for _target, passed in orchestrator.invoke_parameters:
        child = parse_notebook(CHILD, parameters=passed)
        written |= {w.table.table for w in child.writes}
    assert written == {"sales", "order"}


def test_notebook_run_in_a_loop_over_literal_tables():
    result = parse_notebook(
        _notebook(
            'for table in ["sales", "order"]:\n'
            '    notebookutils.notebook.run("nb_merge", 90, {"table": table})'
        )
    )
    assert not result.warnings
    assert sorted(p["table"] for _, p in result.invoke_parameters) == ["order", "sales"]
