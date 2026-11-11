"""
The Job Monitor, kept current: a loop that re-reads what is running.

    from fabribuddy_intenant import live
    live(tenant_id=..., client_id=..., client_secret=...,
         sql_server=..., sql_database=..., sql_token=<a function returning a token>,
         minutes=55, every_seconds=60)

A scan reads every item's history once a day; this keeps the monitor's pages
fresh in between, cheaply. It starts from the scanner's saved state (so the
chain, the code and the schedules are the scan's), reads every runnable item
once, then every `every_seconds`:

- **the hot items** — those running or queued, everything their chain is about
  to start, and those a schedule starts within minutes — and pipeline activity
  runs still in progress;
- **a slice of the rest**, so every item is read at least every
  `sweep_minutes` (a manual run nobody scheduled still shows up);

and rewrites only the monitor's answers (`/monitor/jobs`, `/monitor/runs`,
`/monitor/live` and the pages of the items that changed, with everything above
them). The app polls `/monitor/live` and reloads when it moves.

It never saves the scanner's state: the daily scan stays the one writer of it,
and re-reads all of this anyway.

Read-only against Fabric, like the scan: the same service principal, the same
GET-only client.
"""

import logging
import time
from collections.abc import Callable
from pathlib import Path

from . import payloads, sqldb
from .runner import _configure, _migrate

log = logging.getLogger("fabribuddy.intenant.live")


def live(
    *,
    sql_server: str,
    sql_database: str,
    sql_token: str | Callable[[], str],
    tenant_id: str,
    client_id: str,
    client_secret: str,
    minutes: float = 55,
    every_seconds: int = 60,
    sweep_minutes: int = 15,
    workdir: str = "/tmp/fabribuddy-live",
) -> dict:
    """Refresh the Job Monitor every `every_seconds` for `minutes`."""
    if not (tenant_id and client_id and client_secret):
        raise ValueError("The service principal is needed: tenant_id, client_id and client_secret.")
    started = time.monotonic()
    deadline = started + minutes * 60
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    db_file = _configure(work, client_secret, demo=False)
    token = sql_token if callable(sql_token) else (lambda: sql_token)

    def say(message: str) -> None:
        print(f"[{time.monotonic() - started:6.1f}s] {message}", flush=True)

    conn = sqldb.connect(sql_server, sql_database, token())
    state, conn = _wait_for_scan(conn, sql_server, sql_database, token, deadline, say)
    if state is None:
        say(
            "No scan has finished yet, so there is nothing to refresh. The first scan "
            "(Load_FabriBuddy_E2E) takes 10-30 minutes; the next run of this notebook picks it up."
        )
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return {"ticks": 0, "pages": 0, "errors": 0, "waited": True, "seconds": round(time.monotonic() - started, 1)}
    db_file.write_bytes(state)
    _migrate(db_file)
    say(f"Restored the last scan ({len(state) / 2**20:.1f} MB)")

    from fabribuddy_core import config, db, repository
    from fabribuddy_core.fabric.client import FabricClient
    from fabribuddy_core.lineage import monitor, views
    from fabribuddy_core.lineage.collectors import jobs
    from fabribuddy_core.services.connections import credentials_for
    from fabribuddy_core.services.lineage import _contributor_workspaces

    from . import export

    session = db.SessionLocal()
    tenants = repository.list_tenants(session, config.DEFAULT_CUSTOMER_ID)
    if not tenants:
        raise RuntimeError("The saved state has no tenant. Run Load_FabriBuddy_E2E once.")
    tenant = tenants[0]
    # _configure put the secret in the environment variable the tenant's
    # secret_ref names, as for a scan.
    client = FabricClient(credentials_for(tenant))
    scope = _contributor_workspaces(tenant)
    every_item = monitor.item_ids(views.Context(session, tenant.customer_id, tenant.id))
    per_tick = max(1, -(-len(every_item) * every_seconds // (sweep_minutes * 60)))
    cursor = 0
    ticks, published, errors = 0, 0, 0
    first = True

    try:
        while True:
            tick = time.monotonic()
            session.expire_all()
            context = views.Context(session, tenant.customer_id, tenant.id)
            if first:
                # Everything once: the state is the last scan's, and pages a
                # previous live loop already moved on must not step back.
                wanted = set(every_item)
            else:
                wanted = monitor.hot_items(context)
                for _ in range(per_tick):
                    if every_item:
                        wanted.add(every_item[cursor % len(every_item)])
                        cursor += 1
            try:
                result = jobs.collect(
                    session,
                    client,
                    customer_id=tenant.customer_id,
                    tenant_id=tenant.id,
                    contributor_workspaces=scope,
                    only_items=wanted,
                    with_schedules=first,
                )
            except Exception as e:  # noqa: BLE001 — one bad minute must not end the loop
                errors += 1
                say(f"Read failed ({type(e).__name__}: {str(e)[:200]}); trying again next minute")
                result = {"changed": [], "stats": {}}
            changed = set(result.get("changed") or [])
            session.expire_all()
            pages = (
                every_item
                if first
                else sorted(monitor.ancestors(views.Context(session, tenant.customer_id, tenant.id), changed))
            )
            answers, _stats = export.collect(monitor_items=pages)
            source = {
                "kind": "live",
                "everySeconds": every_seconds,
                "until": _utc_in(deadline - time.monotonic()),
            }
            if isinstance(answers.get("/monitor/live"), dict):
                answers["/monitor/live"]["source"] = source
            rows = [
                (payloads.key(url), part, chunk)
                for url, value in answers.items()
                for part, chunk in enumerate(payloads.pack(value))
            ]
            try:
                sqldb.put_chunks(conn, rows, [payloads.key(u) for u in answers])
            except Exception:  # noqa: BLE001 — the token expired, or the connection dropped
                conn = _reconnect(conn, sql_server, sql_database, token)
                sqldb.put_chunks(conn, rows, [payloads.key(u) for u in answers])
            ticks += 1
            published += len(answers)
            say(
                f"Read {len(wanted)} item(s), {len(changed)} changed; "
                f"wrote {len(answers)} page(s) in {time.monotonic() - tick:.1f}s"
            )
            first = False
            wait = every_seconds - (time.monotonic() - tick)
            if time.monotonic() + max(wait, 0) >= deadline:
                break
            if wait > 0:
                time.sleep(wait)
    finally:
        session.close()
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return {
        "ticks": ticks,
        "pages": published,
        "errors": errors,
        "seconds": round(time.monotonic() - started, 1),
    }


def _wait_for_scan(conn, server, database, token, deadline: float, say, poll_seconds: int = 60):
    """The scanner's saved state; until the first scan has saved it, check again every minute.

    On a first deployment FabriBuddy_Live starts right away, while the first scan is still
    running. Waiting for it (instead of failing) lets the same run go live as soon as it ends.
    Returns (state, connection); the state is None if no scan finishes before `deadline`.
    """
    said = False
    while True:
        try:
            state = sqldb.load_state(conn)
        except Exception:  # noqa: BLE001 — the token expired, or the connection dropped
            conn = _reconnect(conn, server, database, token)
            state = sqldb.load_state(conn)
        if state:
            return state, conn
        if time.monotonic() + poll_seconds >= deadline:
            return None, conn
        if not said:
            say("Waiting for the first scan (Load_FabriBuddy_E2E) to finish; checking every minute")
            said = True
        time.sleep(poll_seconds)


def _reconnect(conn, server: str, database: str, token: Callable[[], str]):
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    return sqldb.connect(server, database, token())


def _utc_in(seconds: float) -> str:
    import datetime as dt

    at = dt.datetime.utcnow() + dt.timedelta(seconds=max(seconds, 0))
    return at.replace(microsecond=0).isoformat() + "Z"
