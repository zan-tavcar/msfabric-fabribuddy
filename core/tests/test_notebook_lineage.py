"""Synthetic notebook fixtures: no tenant identifiers or production SQL."""

import base64
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fabribuddy_core.fabric.definitions import get_definition, is_relevant
from fabribuddy_core.lineage import resolve
from fabribuddy_core.lineage.collectors import definitions
from fabribuddy_core.lineage.parsers.notebook import parse_notebook, split_cells


SQL_NOTEBOOK = """-- Fabric notebook source

-- METADATA ********************
-- META {
-- META   "dependencies": {
-- META     "lakehouse": {
-- META       "default_lakehouse_name": "example_lakehouse"
-- META     }
-- META   }
-- META }

-- MARKDOWN ********************
-- Example documentation, not executable SQL.

-- CELL ********************
CREATE OR REPLACE MATERIALIZED LAKE VIEW dbo.example_output
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')
AS SELECT * FROM dbo.example_input;

-- METADATA ********************
-- META {"language": "sparksql"}
"""


@pytest.mark.parametrize("suffix", ["py", "sql"])
def test_notebook_definition_parts_are_retained(suffix):
    assert is_relevant("Notebook", f"notebook-content.{suffix}")
    assert not is_relevant("Notebook", ".platform")


def test_sql_notebook_cells_preserve_lakehouse_metadata_and_numbering():
    cells, metadata = split_cells(SQL_NOTEBOOK)
    assert len(cells) == 1
    assert cells[0].label == "cell 2"
    assert "CREATE OR REPLACE" in cells[0].text
    assert metadata["dependencies"]["lakehouse"]["default_lakehouse_name"] == (
        "example_lakehouse"
    )


def test_native_sql_notebook_has_qualified_mlv_lineage():
    result = parse_notebook(SQL_NOTEBOOK)
    assert not result.warnings
    assert len(result.writes) == 1
    write = result.writes[0]
    assert write.table.key() == "example_lakehouse.dbo.example_output"
    assert write.mode == "materialized_view"
    assert {source.key() for source in write.reads} == {
        "example_lakehouse.dbo.example_input"
    }
    assert write.via_ref == "cell 2"


@pytest.mark.parametrize(
    "cell",
    [
        "%%sql\nCREATE TABLE dbo.example_output AS SELECT * FROM dbo.example_input;",
        'spark.sql("CREATE TABLE dbo.example_output AS SELECT * FROM dbo.example_input")',
    ],
)
def test_python_notebook_sql_lineage_is_preserved(cell):
    source = '''# Fabric notebook source
# METADATA ********************
# META {"dependencies": {"lakehouse": {"default_lakehouse_name": "example_lakehouse"}}}
# CELL ********************
''' + cell
    result = parse_notebook(source)
    assert not result.warnings
    assert len(result.writes) == 1
    assert result.writes[0].table.key() == "example_lakehouse.dbo.example_output"


def _notebook():
    return SimpleNamespace(
        id="example-notebook",
        workspace_id="example-workspace",
        item_type="Notebook",
        display_name="Example notebook",
    )


def _artifacts(source=SQL_NOTEBOOK):
    return [SimpleNamespace(part_path="notebook-content.sql", text=source)]


def test_collector_dispatches_sql_notebooks():
    result, extra = definitions._run_parser(_notebook(), _artifacts())
    assert extra is None
    assert result is not None
    assert not result.warnings
    assert result.writes[0].table.key() == "example_lakehouse.dbo.example_output"


def test_dispatch_passes_language_for_sql_without_export_markers():
    source = (
        "CREATE TABLE example_lakehouse.dbo.example_output "
        "AS SELECT * FROM example_lakehouse.dbo.example_input;"
    )
    result, _ = definitions._run_parser(_notebook(), _artifacts(source))
    assert result is not None
    assert result.writes[0].table.key() == "example_lakehouse.dbo.example_output"


def test_context_retains_sql_notebooks():
    notebook = _notebook()
    context = definitions._Context.build(
        {notebook.id: notebook},
        {},
        {notebook.id: _artifacts()},
        SimpleNamespace(nodes=[]),
    )
    assert context.notebooks[notebook.workspace_id][notebook.display_name] == SQL_NOTEBOOK


def test_pipeline_runs_parse_sql_notebooks(monkeypatch):
    notebook = _notebook()
    apply_result = Mock()
    monkeypatch.setattr(definitions.resolve, "apply_parse_result", apply_result)
    errors = []
    count = definitions._apply_parameter_runs(
        SimpleNamespace(begin_nested=nullcontext),
        SimpleNamespace(),
        [(SimpleNamespace(), "Example notebook", {})],
        SimpleNamespace(find=lambda pipeline, reference: notebook),
        {notebook.id: _artifacts()},
        definitions._Context(),
        {},
        errors,
    )
    assert count == 1
    assert not errors
    apply_result.assert_called_once()
    result = apply_result.call_args.args[2]
    assert result.writes[0].mode == "materialized_view"
    assert result.writes[0].table.key() == "example_lakehouse.dbo.example_output"


def test_definition_fetch_decodes_sql_and_ignores_platform_metadata():
    payload = base64.b64encode(SQL_NOTEBOOK.encode()).decode()
    client = SimpleNamespace(
        request=Mock(),
        poll_lro=Mock(return_value={
            "definition": {"parts": [
                {"path": "notebook-content.sql", "payload": payload},
                {"path": ".platform", "payload": payload},
            ]}
        }),
    )
    parts = get_definition(client, "example-workspace", "example-notebook", "Notebook")
    assert parts == {"notebook-content.sql": SQL_NOTEBOOK}


def test_sql_cells_without_language_metadata_use_the_notebook_language():
    source = SQL_NOTEBOOK.rsplit("-- METADATA", 1)[0]
    source += """
-- CELL ********************
CREATE TABLE dbo.example_second AS SELECT * FROM dbo.example_output;
"""
    result = parse_notebook(source)
    assert not result.warnings
    assert [write.via_ref for write in result.writes] == ["cell 2", "cell 3"]


def test_cell_language_overrides_the_notebook_language():
    source = '''# Fabric notebook source
# METADATA ********************
# META {"dependencies": {"lakehouse": {"default_lakehouse_name": "example_lakehouse"}}}
# CELL ********************
CREATE TABLE dbo.example_output AS SELECT * FROM dbo.example_input;
# METADATA ********************
# META {"language": "sparksql"}
# CELL ********************
spark.sql("CREATE TABLE dbo.example_second AS SELECT * FROM dbo.example_output")
# METADATA ********************
# META {"language": "python"}
'''
    result = parse_notebook(source, language="python")
    assert not result.warnings
    assert [write.via_ref for write in result.writes] == ["cell 1", "cell 2"]


def test_mlv_write_resolves_to_existing_catalog_nodes(monkeypatch):
    lakehouse = SimpleNamespace(
        id="example-lakehouse", item_guid="example-lakehouse-guid",
        display_name="example_lakehouse", workspace_id="example-workspace",
        item_type="Lakehouse",
    )
    workspace = SimpleNamespace(
        id="example-workspace", workspace_guid="example-workspace-guid",
        name="Example workspace",
    )
    nodes = [
        SimpleNamespace(
            id=f"example-{name}-node", item_id=lakehouse.id,
            workspace_id=workspace.id, schema_name="dbo",
            table_name=f"example_{name}", kind="lakehouse",
        )
        for name in ("input", "output")
    ]
    monkeypatch.setattr(resolve.repository, "list_nodes", lambda *args: nodes)
    monkeypatch.setattr(resolve.repository, "list_items", lambda *args: [lakehouse])
    monkeypatch.setattr(resolve.repository, "list_workspaces", lambda *args: [workspace])
    monkeypatch.setattr(resolve.Resolver, "_load_columns", lambda self: [])
    monkeypatch.setattr(resolve.config, "COLUMN_LINEAGE", False)
    store_edge = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(resolve.repository, "upsert_edge", store_edge)
    resolver = resolve.Resolver(None, "example-customer", "example-tenant")
    result = parse_notebook(SQL_NOTEBOOK)
    stats = resolve.apply_parse_result(
        None, resolver, result, via_item_id="example-notebook",
        workspace_guid=workspace.workspace_guid,
    )
    assert stats["edges"] == 1
    assert stats["unresolved"] == 0
    store_edge.assert_called_once()
    assert store_edge.call_args.kwargs["from_node_id"] == "example-input-node"
    assert store_edge.call_args.kwargs["to_node_id"] == "example-output-node"
    assert store_edge.call_args.kwargs["via_item_id"] == "example-notebook"
    assert store_edge.call_args.kwargs["write_mode"] == "materialized_view"