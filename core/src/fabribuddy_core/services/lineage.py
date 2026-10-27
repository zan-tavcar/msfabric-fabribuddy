"""
Running collectors against a stored connection, and serving what they wrote.

Mirrors the collector contract: each runs as a recorded `collector_run`, writes
normalised rows, and reports per-item failures as `collector_error` rows rather
than raising. A run that hit some failures finishes `partial`, not `failed` —
the difference matters because `partial` still has usable data behind it.
"""

import logging

from sqlalchemy.orm import Session

from .. import entitlements, repository, secrets
from ..fabric import items as items_api
from ..fabric import scanner as scanner_api
from ..fabric.client import FabricClient, FabricError
from ..lineage import findings as findings_engine
from ..lineage import repository as lineage_repository
from ..lineage import tree as tree_builder
from ..lineage.collectors import access as access_collector
from ..lineage.collectors import definitions as definitions_collector
from ..lineage.collectors import delta as delta_collector
from ..lineage.collectors import items as items_collector
from ..lineage.collectors import jobs as jobs_collector
from ..lineage.collectors import scanner as scanner_collector
from ..models import (
    KIND_ACCESS,
    KIND_DEFINITIONS,
    KIND_DELTA,
    KIND_ITEMS,
    KIND_JOBS,
    KIND_PARSE,
    KIND_SCANNER,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_PARTIAL,
    ConnectedTenant,
)
from ..redaction import scrub
from .connections import ConnectionError_, credentials_for

log = logging.getLogger("lineage.service")


class CollectionFailed(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class CollectionAlreadyRunning(CollectionFailed):
    """A run of this kind is already in flight — a conflict, not a failure."""


def _secret_for_scrubbing(tenant: ConnectedTenant) -> str | None:
    """
    Best-effort read of the tenant's secret, purely so errors can be scrubbed.

    Resolved BEFORE the work starts rather than from the credentials object,
    because an exception raised while building those credentials can itself
    carry the secret — and that error is about to be written to a database row.
    Never raises: if the secret cannot be read there is nothing to redact.
    """
    try:
        return secrets.resolve(tenant.secret_ref)
    except Exception:  # noqa: BLE001 — redaction must never be the thing that fails
        return None


def _execute(session: Session, tenant: ConnectedTenant, kind: str, params: dict, fn):
    """Run `fn(client)` as a recorded collector_run, persisting success or failure."""
    run = repository.start_run(session, tenant, kind, params)
    secret = _secret_for_scrubbing(tenant)
    try:
        creds = credentials_for(tenant)
        result = fn(FabricClient(creds))
    except (FabricError, ConnectionError_) as e:
        message = scrub(str(e), secret) or "Collection failed."
        repository.finish_run(session, run, status=STATUS_FAILED, error=message)
        raise CollectionFailed(message) from None  # drop the chain: it can hold creds
    except Exception as e:
        # A bug (unexpected payload shape, a DB error mid-upsert) must still
        # close the run. Left 'running', it blocks every later run of this kind
        # until a restart reaps it, and the UI shows a scan that never ends.
        log.error(
            "%s collection crashed: %s", kind, scrub(f"{type(e).__name__}: {e}", secret)
        )
        session.rollback()  # a failed flush leaves the session unusable otherwise
        message = scrub(f"Internal error: {type(e).__name__}: {e}", secret)[:500]
        repository.finish_run(session, run, status=STATUS_FAILED, error=message)
        raise CollectionFailed(message) from None

    errors = [
        {**e, "message": scrub(str(e.get("message", "")), secret)}
        for e in result.get("errors", [])
    ]
    repository.record_errors(session, run, errors)
    repository.finish_run(
        session,
        run,
        status=STATUS_PARTIAL if errors else STATUS_OK,
        stats=result.get("stats", {}),
    )
    return result


def _contributor_workspaces(tenant: ConnectedTenant) -> set[str]:
    return {w.lower() for w in (tenant.contributor_workspaces or [])}


# --- scope -----------------------------------------------------------------
def refresh_scope(
    session: Session, tenant: ConnectedTenant, client: FabricClient
) -> set[str]:
    """
    Set the tenant's scope to the workspaces where the SPN is at least Contributor.

    Every collector reads only these. Asked fresh at the start of each pass, so
    a role granted or revoked in Fabric takes effect on the next scan without
    anyone editing a list. An empty answer is kept as-is rather than falling
    back to the whole tenant: no Contributor role means nothing to document.
    """
    guids = sorted(
        (ws.get("id") or "").lower()
        for ws in items_api.list_workspaces(client, roles=items_api.CONTRIBUTOR_ROLES)
        if ws.get("id")
    )
    if (tenant.scope_mode or "all") == "selected":
        # The Admin page narrowed the scope. A selected workspace the SPN lost
        # Contributor on simply drops out; it is never scanned without the role.
        chosen = {w.lower() for w in tenant.selected_workspaces or []}
        guids = [g for g in guids if g in chosen]
    # The trial's hard cap, applied to what is actually scanned.
    guids = entitlements.cap_scope(session, tenant.customer_id, guids)
    repository.set_contributor_workspaces(session, tenant, guids)
    purged = lineage_repository.purge_workspaces_outside(
        session, tenant.customer_id, tenant.id, set(guids)
    )
    log.info(
        "scope: %d workspace(s) where the SPN is at least Contributor; %d removed",
        len(guids),
        purged,
    )
    return set(guids)


# --- collectors ------------------------------------------------------------
def run_scanner(
    session: Session, tenant: ConnectedTenant, since: str | None = None
) -> dict:
    """
    Power BI Scanner API over the Contributor scope only.

    `since` uses the `modified` endpoint for an incremental pass, intersected
    with the scope, so a nightly run costs a handful of getInfo calls.
    """

    def work(client: FabricClient):
        scope = refresh_scope(session, tenant, client)
        if since:
            modified = {
                w.lower() for w in scanner_api.modified_workspace_ids(client, since)
            }
            workspace_ids = sorted(scope & modified)
        else:
            workspace_ids = sorted(scope)
        return scanner_collector.collect(
            session,
            client,
            customer_id=tenant.customer_id,
            tenant_id=tenant.id,
            workspace_ids=workspace_ids,
            contributor_workspaces=scope,
        )

    return _execute(session, tenant, KIND_SCANNER, {"since": since}, work)


def run_items(session: Session, tenant: ConnectedTenant) -> dict:
    """Fabric REST inventory plus lakehouse tables via the OneLake table API."""

    def work(client: FabricClient):
        return items_collector.collect(
            session,
            client,
            customer_id=tenant.customer_id,
            tenant_id=tenant.id,
            contributor_workspaces=_contributor_workspaces(tenant),
        )

    return _execute(session, tenant, KIND_ITEMS, {}, work)


def run_definitions(
    session: Session, tenant: ConnectedTenant, incremental: bool = False
) -> dict:
    """
    getDefinition for parseable items in scope, stored as code_artifact.

    Incremental: an item whose last-modified time is not newer than its stored
    code is skipped. Items without a timestamp (semantic models, reports) are
    re-fetched only when their workspace changed since the last successful run,
    per the Scanner's `modified` API — getDefinition is the slow, rate-limited
    call, so this is what makes a daily scan take minutes instead of a quarter
    of an hour.
    """
    previous = repository.latest_run(
        session, tenant.customer_id, tenant.id, KIND_DEFINITIONS, only_ok=True
    )

    def work(client: FabricClient):
        changed = None
        if incremental and previous is not None and previous.started_at is not None:
            try:
                since = previous.started_at.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
                changed = {
                    w.lower() for w in scanner_api.modified_workspace_ids(client, since)
                }
            except FabricError as e:
                log.info("modified-workspaces lookup failed, fetching all: %s", e)
        return definitions_collector.fetch(
            session,
            client,
            customer_id=tenant.customer_id,
            tenant_id=tenant.id,
            contributor_workspaces=_contributor_workspaces(tenant),
            incremental=incremental and previous is not None,
            changed_workspaces=changed,
        )

    return _execute(session, tenant, KIND_DEFINITIONS, {"incremental": incremental}, work)


def run_warehouse_sql(session: Session, tenant: ConnectedTenant) -> dict:
    """T-SQL modules and recent write statements from warehouses and SQL endpoints."""
    from ..lineage.collectors import warehouse_sql as warehouse_sql_collector
    from ..models import KIND_WAREHOUSE_SQL

    def work(client: FabricClient):
        return warehouse_sql_collector.collect(
            session,
            client,
            customer_id=tenant.customer_id,
            tenant_id=tenant.id,
            azure_tenant_id=tenant.azure_tenant_id,
            contributor_workspaces=_contributor_workspaces(tenant),
        )

    return _execute(session, tenant, KIND_WAREHOUSE_SQL, {}, work)


def run_parse(session: Session, tenant: ConnectedTenant, force: bool = False) -> dict:
    """Parsers and resolver over stored artifacts. No network."""

    def work(_client: FabricClient):
        result = definitions_collector.parse(
            session, customer_id=tenant.customer_id, tenant_id=tenant.id, force=force
        )
        return _with_attribution(session, tenant, result)

    return _execute(session, tenant, KIND_PARSE, {"force": force}, work)


def _with_attribution(session: Session, tenant: ConnectedTenant, result: dict) -> dict:
    """
    Add dataflow attribution for writerless tables to a step's result.

    It rests on two things: the parse (which dataflow queries exist) and the
    Delta log (which engine wrote a table). So it runs after each of them, and
    whichever ran last has the freshest evidence.
    """
    attributed = definitions_collector.attribute_dormant_writes(
        session, customer_id=tenant.customer_id, tenant_id=tenant.id
    )
    stats = dict(result.get("stats", {}))
    stats["attributedFromDelta"] = attributed["stats"]["tables"]
    return {
        **result,
        "stats": stats,
        "errors": list(result.get("errors", [])) + attributed["errors"],
    }


def run_delta(session: Session, tenant: ConnectedTenant) -> dict:
    """Delta log over OneLake for every lakehouse table in scope."""

    from .. import config

    def work(client: FabricClient):
        if config.DELTA_SCAN == "off":
            return _with_attribution(
                session,
                tenant,
                {"stats": {"skipped": "FL_DELTA_SCAN=off"}, "errors": []},
            )
        result = delta_collector.collect(
            session,
            client,
            customer_id=tenant.customer_id,
            tenant_id=tenant.id,
            contributor_workspaces=_contributor_workspaces(tenant),
        )
        return _with_attribution(session, tenant, result)

    return _execute(session, tenant, KIND_DELTA, {}, work)


def run_jobs(session: Session, tenant: ConnectedTenant) -> dict:
    """Job-instance history for runnable items in scope. Collection only."""

    def work(client: FabricClient):
        return jobs_collector.collect(
            session,
            client,
            customer_id=tenant.customer_id,
            tenant_id=tenant.id,
            contributor_workspaces=_contributor_workspaces(tenant),
        )

    return _execute(session, tenant, KIND_JOBS, {}, work)


def run_access(
    session: Session, tenant: ConnectedTenant, scanner_access: dict | None
) -> dict:
    """
    Access grants from the Scanner's user lists, then the findings engine.

    Needs the user lists from a scanner pass in the same refresh; they are not
    stored, so a standalone access run has nothing to resolve and says so.
    Findings run here because they read the grants this step just wrote.
    """

    def work(_client: FabricClient):
        scanner_access_ = scanner_access or {}
        result = access_collector.collect(
            session,
            None,
            customer_id=tenant.customer_id,
            tenant_id=tenant.id,
            workspace_users=scanner_access_.get("workspace_users"),
            item_users=scanner_access_.get("item_users"),
        )
        errors = list(result["errors"])
        if not scanner_access:
            errors.append(
                {
                    "scope": "tenant",
                    "scope_name": "access",
                    "reason": "unsupported",
                    "message": "No Scanner user lists in this pass — run a full refresh.",
                }
            )
        # Neither is collected yet; say so instead of implying the grants are complete.
        errors.append(
            {
                "scope": "tenant",
                "scope_name": "OneLake roles and group members",
                "reason": "unsupported",
                "message": (
                    "OneLake data access roles and Entra group membership are not "
                    "collected yet, so grants through them are missing and groups "
                    "are shown unexpanded."
                ),
            }
        )
        found = findings_engine.run_all(session, tenant.customer_id, tenant.id)
        session.commit()
        stats = {**result["stats"], **{f"finding_{k}": v for k, v in found.items()}}
        return {"stats": stats, "errors": errors}

    return _execute(session, tenant, KIND_ACCESS, {}, work)


def run_inventory(session: Session, tenant: ConnectedTenant) -> dict:
    """
    Both Phase-1 collectors, scanner first.

    Order matters: the scanner establishes workspaces and governance metadata
    (sensitivity labels, endorsement) that the items collector would otherwise
    have no way to fill in, and upsert_item deliberately will not blank a field
    a previous collector populated.
    """
    stats = {}
    try:
        stats["scanner"] = run_scanner(session, tenant)["stats"]
    except CollectionFailed as e:
        # The items collector may still work — a tenant can have the Fabric API
        # available without the admin-API tenant setting.
        stats["scanner"] = {"error": e.message}
    stats["items"] = run_items(session, tenant)["stats"]
    return stats


# The steps of a full refresh, in order (for progress).
STEP_NAMES = (
    "scanner",
    "items",
    "definitions",
    "warehouse_sql",
    "parse",
    "delta",
    "jobs",
    "access",
)


def run_full_refresh(
    session: Session, tenant: ConnectedTenant, incremental: bool = False, on_step=None
) -> dict:
    """
    Every collector, in dependency order, each recorded as its own run.

    scanner -> items -> definitions -> warehouse SQL -> parse -> delta -> jobs
    -> access. `incremental` re-fetches only changed definitions; everything
    downstream is incremental already (parse skips unchanged code, the Delta
    reader resumes from the last version it saw).
    Parse follows definitions because it reads what they stored; delta follows
    items because it walks the lakehouse tables items enumerated; access goes
    last because the findings it triggers read everything before it. One step
    failing is recorded and the rest still run — a missing Delta log should not
    cost the customer their lineage.
    """
    stats: dict = {}
    scanner_access = None
    if on_step is not None:
        on_step("scanner")
    try:
        result = run_scanner(session, tenant)
        stats["scanner"] = result["stats"]
        scanner_access = result.get("access")
    except CollectionFailed as e:
        stats["scanner"] = {"error": e.message}

    steps = [
        ("items", lambda: run_items(session, tenant)),
        ("definitions", lambda: run_definitions(session, tenant, incremental)),
        ("warehouse_sql", lambda: run_warehouse_sql(session, tenant)),
        ("parse", lambda: run_parse(session, tenant)),
        ("delta", lambda: run_delta(session, tenant)),
        ("jobs", lambda: run_jobs(session, tenant)),
        ("access", lambda: run_access(session, tenant, scanner_access)),
    ]
    for name, step in steps:
        log.info("refresh: %s", name)
        if on_step is not None:
            on_step(name)
        try:
            stats[name] = step()["stats"]
        except CollectionFailed as e:
            stats[name] = {"error": e.message}
    log.info("refresh finished: %s", {k: v for k, v in stats.items()})
    return stats


def discover_contributor_workspaces(session: Session, tenant: ConnectedTenant) -> list[str]:
    """
    Probe which workspaces the SPN can actually reach at Contributor tier.

    A convenience for the connect flow: it turns "tick the workspaces you gave
    us access to" into a pre-filled list. It never grants anything — the
    customer still confirms, because opting a workspace in is their decision.
    """
    creds = credentials_for(tenant)
    client = FabricClient(creds)
    reachable = []
    for ws in items_api.list_workspaces(client):
        guid = (ws.get("id") or "").lower()
        if guid and items_api.probe_contributor(client, guid):
            reachable.append(guid)
    return reachable


# --- reads -----------------------------------------------------------------
def get_tree(session: Session, tenant: ConnectedTenant) -> dict:
    """
    The sidebar hierarchy. NEVER starts a collection.

    Reads are strictly reads: a full inventory pass is minutes of work against
    rate-limited APIs, and lazy-triggering it from a page load would launch a
    new one on every render.
    """
    return tree_builder.build(session, tenant.customer_id, tenant.id)


def collection_status(session: Session, tenant: ConnectedTenant) -> dict:
    """What each collector last did — what the UI polls while a run is going."""
    from ..models import ALL_KINDS

    out = []
    for kind in ALL_KINDS:
        running = repository.running_run(session, tenant.customer_id, tenant.id, kind)
        last = repository.latest_run(
            session, tenant.customer_id, tenant.id, kind, only_ok=False
        )
        out.append(
            {
                "kind": kind,
                "running": running is not None,
                "startedAt": running.started_at.isoformat() if running else None,
                "lastRunAt": last.started_at.isoformat() if last else None,
                "lastStatus": last.status if last else None,
                "lastError": last.error if last else None,
                "stats": last.stats if last else {},
            }
        )
    return {"collectors": out, "anyRunning": any(c["running"] for c in out)}
