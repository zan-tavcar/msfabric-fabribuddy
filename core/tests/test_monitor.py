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


def test_live_waits_for_the_first_scan_instead_of_failing(monkeypatch, tmp_path):
    """FabriBuddy_Live started before the first scan has saved its state: it waits, then ends cleanly."""
    import importlib

    live_mod = importlib.import_module("fabribuddy_intenant.live")

    calls = {"load": 0}

    class Conn:
        def close(self):
            pass

    def load_state(conn):
        calls["load"] += 1
        return None

    clock = {"t": 0.0}
    monkeypatch.setattr(live_mod.sqldb, "connect", lambda *a, **k: Conn())
    monkeypatch.setattr(live_mod.sqldb, "load_state", load_state)
    monkeypatch.setattr(live_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(live_mod.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(live_mod, "_configure", lambda work, secret, demo: tmp_path / "state.db")

    result = live_mod.live(
        sql_server="s", sql_database="d", sql_token="t",
        tenant_id="t", client_id="c", client_secret="x",
        minutes=5, workdir=str(tmp_path),
    )
    assert result["waited"] is True and result["ticks"] == 0
    assert calls["load"] == 5  # checked every minute until the run's time was up


def test_live_wait_returns_the_state_once_the_scan_has_saved_it(monkeypatch):
    import importlib

    live_mod = importlib.import_module("fabribuddy_intenant.live")

    states = iter([None, None, b"state"])
    monkeypatch.setattr(live_mod.sqldb, "load_state", lambda conn: next(states))
    clock = {"t": 0.0}
    monkeypatch.setattr(live_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(live_mod.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    state, conn = live_mod._wait_for_scan("conn", "s", "d", lambda: "t", deadline=3600, say=lambda m: None)
    assert state == b"state" and conn == "conn" and clock["t"] == 120


def test_a_wrong_secret_is_reported_as_a_sign_in_problem_not_the_admin_api_setting(monkeypatch):
    from fabribuddy_core.fabric import client as fc
    from fabribuddy_core.fabric.credentials import Credentials

    class App:
        def acquire_token_for_client(self, scopes):
            return {
                "error": "invalid_client",
                "error_description": "AADSTS7000215: Invalid client secret provided. Ensure the secret "
                "being sent in the request is the client secret value, not the client secret ID.",
            }

    provider = fc.TokenProvider(Credentials(tenant_id="t", client_id="c", client_secret="wrong"))
    monkeypatch.setattr(provider, "_application", lambda: App())
    try:
        provider.token(fc.SCOPE_POWERBI)
    except fc.SignInFailed as e:
        message = str(e)
    else:
        raise AssertionError("expected SignInFailed")
    assert "client secret is wrong" in message and "AADSTS7000215" in message
    assert "admin APIs" not in message
    # Still an AccessDenied, so every collector that catches that keeps working.
    assert issubclass(fc.SignInFailed, fc.AccessDenied)


def test_admin_api_problem_names_the_sign_in_fix(monkeypatch):
    from fabribuddy_core.fabric.client import SignInFailed
    from fabribuddy_core.services import connections

    class Tenant:
        is_demo = False

    def fail(creds):
        raise SignInFailed("The service principal couldn't sign in. The client secret is wrong.")

    monkeypatch.setattr(connections, "credentials_for", lambda t: None)
    monkeypatch.setattr(connections, "probe_powerbi_admin", fail)
    problem = connections.admin_api_problem(Tenant())
    assert problem.startswith("The service principal couldn't sign in") and "admin APIs" not in problem
