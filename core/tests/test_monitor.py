"""
The Activity monitor on the Contoso demo: the live answer, which items the live
refresher reads, a partial read, and the monitor-only export.

Each check runs in a fresh process: fabribuddy_core reads its settings once, on
import (the runner's _configure must come first).
"""

import json
import subprocess
import sys
import textwrap

PRELUDE = """
import json, tempfile
from pathlib import Path
from fabribuddy_intenant import runner
work = Path(tempfile.mkdtemp())
db_file = runner._configure(work, "", demo=True)
runner._migrate(db_file)
from fabribuddy_core import config, db, demo_org, repository
from fabribuddy_core.lineage import monitor, views
session = db.SessionLocal()
demo_org._build(session)
tenant = repository.list_tenants(session, config.DEFAULT_CUSTOMER_ID)[0]
context = views.Context(session, tenant.customer_id, tenant.id)
name = {i.id: i.display_name for i in context.items.values()}
by_name = {v: k for k, v in name.items()}
"""


def _run(body: str) -> dict:
    code = PRELUDE + textwrap.dedent(body)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_live_lists_what_runs_now_with_its_current_step():
    out = _run(
        """
        live = monitor.live(context)
        print(json.dumps({
            "running": {r["item"]["name"]: r["activeSteps"] for r in live["running"]},
            "expected": [r["expectedSec"] for r in live["running"]],
            "upcoming": [u["at"] for u in live["upcoming"]],
            "source": live["source"]["kind"],
        }))
        """
    )
    assert out["running"]["pl_master"] == ["Silver_Orchestration"]
    assert "nb_orchestrate_silver" in out["running"]
    assert all(e and e > 0 for e in out["expected"])
    assert out["upcoming"] == sorted(out["upcoming"]) and out["upcoming"]
    assert out["source"] == "scan"


def test_hot_items_are_the_running_chain_and_ancestors_go_up():
    out = _run(
        """
        hot = {name[i] for i in monitor.hot_items(context)}
        up = {name[i] for i in monitor.ancestors(context, {by_name["df_fx_rates"]})}
        print(json.dumps({"hot": sorted(hot), "up": sorted(up)}))
        """
    )
    # Running, and everything it will start.
    for item in ("pl_master", "nb_orchestrate_silver", "nb_gold_model", "pl_ingest_d365", "nb_silver_sales"):
        assert item in out["hot"], item
    # A dataflow's run shows on its own page and on every page above it.
    assert set(out["up"]) == {"df_fx_rates", "pl_ingest_d365", "pl_master"}


def test_a_partial_read_touches_only_the_asked_items_and_reports_changes():
    out = _run(
        """
        from fabribuddy_core.fabric import items as items_api
        from fabribuddy_core.lineage.collectors import jobs
        from fabribuddy_core.lineage.models import JobRun
        from sqlalchemy import select

        target = by_name["nb_gold_model"]
        item = context.items[target]
        asked = []
        latest = session.scalars(
            select(JobRun).where(JobRun.item_id == target).order_by(JobRun.started_at.desc())
        ).first()

        def instances(client, ws, guid):
            asked.append(guid)
            return [
                {"id": latest.run_guid, "jobType": "RunNotebook", "status": latest.status,
                 "startTimeUtc": latest.started_at.isoformat() + "Z",
                 "endTimeUtc": latest.ended_at.isoformat() + "Z", "invokeType": "Pipeline"},
                {"id": "11111111-2222-3333-4444-555555555555", "jobType": "RunNotebook",
                 "status": "InProgress", "startTimeUtc": "2099-01-01T00:00:00Z", "invokeType": "Manual"},
            ]

        items_api.list_job_instances = instances
        scope = {w.workspace_guid.lower() for w in context.workspaces.values()}
        first = jobs.collect(session, None, customer_id=tenant.customer_id, tenant_id=tenant.id,
                             contributor_workspaces=scope, only_items={target}, with_schedules=False)
        again = jobs.collect(session, None, customer_id=tenant.customer_id, tenant_id=tenant.id,
                             contributor_workspaces=scope, only_items={target}, with_schedules=False)
        print(json.dumps({"asked": asked, "first": [name[i] for i in first["changed"]],
                          "again": again["changed"], "guid": item.item_guid}))
        """
    )
    assert out["asked"] == [out["guid"], out["guid"]]
    assert out["first"] == ["nb_gold_model"]  # a new run
    assert out["again"] == []  # nothing moved since


def test_the_live_export_writes_only_the_monitor_pages():
    out = _run(
        """
        from fabribuddy_intenant import export
        answers, _ = export.collect(monitor_items=[by_name["pl_master"]])
        print(json.dumps(sorted(answers)))
        """
    )
    assert {"/monitor/jobs", "/monitor/runs", "/monitor/live"} <= set(out)
    assert len(out) == 4 and all(u.startswith("/monitor/") for u in out)


def test_data_dependencies_link_the_item_that_writes_a_table_to_the_items_that_read_it():
    out = _run(
        """
        payload = monitor.jobs(context)
        deps = sorted((name[d["from"]], name[d["to"]]) for d in payload["data"])
        gold = next(j for j in payload["jobs"] if j["name"] == "nb_gold_model")
        print(json.dumps({"deps": deps, "reads": gold["reads"], "writes": gold["writes"]}))
        """
    )
    deps = {tuple(d) for d in out["deps"]}
    assert ("df_fx_rates", "nb_gold_model") in deps
    assert ("nb_gold_model", "Sales Model") in deps
    assert ("pl_ingest_d365", "nb_silver_sales") in deps
    assert all(a != b for a, b in deps)
    assert any("fx_rates" in t for t in out["reads"]) and out["writes"]
