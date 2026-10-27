"""
One scan: read the tenant as the service principal, then store what the app shows.

    from fabribuddy_intenant import run
    run(tenant_id=..., client_id=..., client_secret=...,
        sql_server=..., sql_database=..., sql_token=<the runner's SQL token>)

Two identities, on purpose:

- **The service principal** calls Microsoft: the read-only admin APIs, Fabric
  REST, OneLake, warehouse SQL endpoints and (optionally) Microsoft Graph.
- **Whoever runs the pipeline** writes FabriBuddy_DB, with their own token
  (`sql_token`), so the principal needs no role on the FabriBuddy workspace.

In order: restore the scanner's working database from FabriBuddy_DB, bring its
schema up to date (Alembic), connect the tenant, run a pass (Data Catalog, then
Report Audience — the SaaS's own code), save the working database back, and
store every page the app reads.

`demo=True` skips the scan and loads the fictional Contoso tenant instead.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

from . import payloads, sqldb

log = logging.getLogger("fabribuddy.intenant")

SECRET_ENV = "FL_INTENANT_SPN_SECRET"


def _configure(workdir: Path, client_secret: str, demo: bool) -> Path:
    """Environment for `fabribuddy_core.config`; must happen before it is imported."""
    if "fabribuddy_core.config" in sys.modules:
        raise RuntimeError(
            "fabribuddy_core was already imported in this session. Restart the "
            "notebook session (Python kernel) and run again."
        )
    db_file = workdir / "state.db"
    os.environ.update(
        {
            "FL_DB_URL": f"sqlite:///{db_file}",
            "FL_AUTH_REQUIRED": "0",
            "FL_SECRET_STORE": "env",
            "FL_SCHEDULER": "0",
            "FL_BOOTSTRAP_FROM_ENV": "0",
            "FL_DEMO_MODE": "1" if demo else "0",
            "FL_DEMO_ORG": "0",
            "FL_PUBLIC_DEMO": "0",
            "FL_AUTO_CREATE_SCHEMA": "0",
            "FL_LOG_LEVEL": os.environ.get("FL_LOG_LEVEL", "WARNING"),
        }
    )
    if demo:
        # The SaaS's public demo organisation ("Contoso"): both products' data.
        os.environ["FL_CUSTOMER_ID"] = "fabribuddy-demo"
    # Lives in this process's memory only; the database stores "env:<name>".
    os.environ[SECRET_ENV] = client_secret or "demo"
    return db_file


def _migrate(db_file: Path) -> None:
    from alembic import command
    from alembic.config import Config

    import fabribuddy_core

    package = Path(fabribuddy_core.__file__).parent
    cfg = Config()
    cfg.set_main_option("script_location", str(package / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    command.upgrade(cfg, "head")


def _connect_tenant(session, *, tenant_id, client_id, display_name, workspaces, include_personal):
    from fabribuddy_core import config, repository
    from fabribuddy_core.fabric.credentials import Credentials
    from fabribuddy_core.models import STATUS_CONNECTED, STATUS_ERROR
    from fabribuddy_core.services import connections

    creds = Credentials(
        tenant_id=tenant_id, client_id=client_id, client_secret=os.environ[SECRET_ENV]
    )
    ok, caps = connections.check_capabilities(creds)
    tenant = repository.upsert_tenant(
        session,
        config.DEFAULT_CUSTOMER_ID,
        display_name=display_name or tenant_id,
        azure_tenant_id=tenant_id.lower(),
        client_id=client_id,
        secret_ref=f"env:{SECRET_ENV}",
        status=STATUS_CONNECTED if ok else STATUS_ERROR,
        capabilities=caps,
        validation_error=None if ok else caps[connections.CAP_POWERBI_ADMIN].get("reason"),
    )
    if workspaces:
        tenant.scope_mode = "selected"
        tenant.selected_workspaces = sorted({w.strip().lower() for w in workspaces if w.strip()})
    else:
        tenant.scope_mode = "all"
        tenant.selected_workspaces = []
    tenant.include_personal = 1 if include_personal else 0
    session.commit()
    return tenant, caps


def _scan(session, tenant, *, full: bool) -> dict:
    """One pass of both products — the SaaS's scan, minus its plan checks."""
    from fabribuddy_core import plans
    from fabribuddy_core.models import TRIGGER_SCHEDULE
    from fabribuddy_core.services import connections, passes

    problem = connections.admin_api_problem(tenant)
    if problem:
        raise RuntimeError(f"The scan could not start: {problem}")
    outcome = passes.run_pass(
        session,
        tenant,
        incremental=not full,
        products=list(plans.PRODUCTS),
        trigger=TRIGGER_SCHEDULE,
    )
    return {"ok": passes.succeeded(outcome), "products": outcome}


def run(
    *,
    sql_server: str,
    sql_database: str,
    sql_token: str,
    tenant_id: str = "",
    client_id: str = "",
    client_secret: str = "",
    tenant_name: str = "",
    workspaces: list[str] | None = None,
    include_personal: bool = False,
    full: bool = False,
    demo: bool = False,
    workdir: str = "/tmp/fabribuddy",
) -> dict:
    """Scan and publish. Returns a summary; raises when nothing usable was produced."""
    started = time.monotonic()
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    if not demo and not (tenant_id and client_id and client_secret):
        raise ValueError(
            "The service principal is needed: tenant_id, client_id and client_secret "
            "(or demo=True)."
        )

    db_file = _configure(work, client_secret, demo)
    summary: dict = {"demo": demo}

    def step(message: str) -> None:
        print(f"[{time.monotonic() - started:6.1f}s] {message}", flush=True)

    conn = sqldb.connect(sql_server, sql_database, sql_token)
    sqldb.ensure_schema(conn)
    run_id = sqldb.start_run(conn)
    step("Connected to FabriBuddy_DB")

    status = "failed"
    try:
        # 2. Restore the working database.
        state = None if demo else sqldb.load_state(conn)
        if db_file.exists():
            db_file.unlink()
        if state:
            db_file.write_bytes(state)
            step(f"Restored the scanner's state ({len(state) / 2**20:.1f} MB)")
        else:
            step("Demo mode" if demo else "No saved state: this is a first (full) scan")

        # 3. Schema.
        _migrate(db_file)

        from fabribuddy_core import db

        session = db.SessionLocal()
        try:
            if demo:
                # Exactly what app.fabribuddy.com/demo shows: the Sales Data
                # lineage scenario and a stored Report Audience scan.
                from fabribuddy_core import demo_org

                demo_org._build(session)
                step("Loaded the Contoso demo (both products)")
            else:
                # 4. Tenant and scope.
                tenant, caps = _connect_tenant(
                    session,
                    tenant_id=tenant_id,
                    client_id=client_id,
                    display_name=tenant_name,
                    workspaces=workspaces,
                    include_personal=include_personal,
                )
                summary["capabilities"] = {k: v.get("available") for k, v in caps.items()}
                step(f"Service principal access: {summary['capabilities']}")

                # The pass.
                step("Scanning (Data Catalog, then Report Audience) — this takes minutes")
                outcome = _scan(session, tenant, full=full or not state)
                summary["scan"] = outcome
                step(f"Scan finished: ok={outcome.get('ok')}")
        finally:
            session.close()

        # 6. Save the working database (before publishing: a failed publish
        #    must not throw away a 20-minute scan).
        if not demo:
            from fabribuddy_core.db import engine

            engine.dispose()
            size = sqldb.save_state(conn, db_file.read_bytes())
            step(f"Saved the scanner's state ({size / 2**20:.1f} MB compressed)")

        # 7. Publish.
        from . import export

        def progress(done: int, total: int) -> None:
            step(f"Preparing pages: {done} of {total}")

        answers, stats = export.collect(on_progress=progress)
        rows = [
            (payloads.key(url), part, chunk)
            for url, value in answers.items()
            for part, chunk in enumerate(payloads.pack(value))
        ]
        sqldb.replace_chunks(conn, rows)
        summary["published"] = {**stats, "chunks": len(rows)}
        step(f"Published {stats['stored']} answers ({len(rows)} rows)")
        status = "ok"
        return summary
    finally:
        summary["seconds"] = round(time.monotonic() - started, 1)
        try:
            sqldb.finish_run(conn, run_id, status, json.dumps(summary, default=str))
        finally:
            conn.close()
