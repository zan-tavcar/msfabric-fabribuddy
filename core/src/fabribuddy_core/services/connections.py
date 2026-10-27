"""
Connecting a tenant, and turning a stored connection back into credentials.

Validation probes each credential tier separately so the UI can say precisely
which one is missing — "inventory works, code does not" is a far more useful
answer than "connection failed".
"""

import logging

from sqlalchemy.orm import Session

from .. import config, repository, secrets
from ..fabric import items as items_api
from ..fabric.client import (
    API_POWERBI,
    SCOPE_POWERBI,
    SCOPE_STORAGE,
    AccessDenied,
    FabricClient,
    FabricError,
)
from ..fabric.credentials import Credentials
from ..models import STATUS_CONNECTED, STATUS_ERROR, ConnectedTenant
from ..redaction import scrub

log = logging.getLogger("lineage.connections")

CAP_POWERBI_ADMIN = "powerbi_admin"
CAP_FABRIC_API = "fabric_api"
CAP_ONELAKE = "onelake"
# Audience: Microsoft Graph for licences and Entra group members. Optional.
CAP_GRAPH = "graph_licenses"


class ConnectionError_(Exception):
    """The stored connection cannot be turned into working credentials."""


def credentials_for(tenant: ConnectedTenant) -> Credentials:
    try:
        secret = secrets.resolve(tenant.secret_ref)
    except secrets.SecretUnavailable as e:
        raise ConnectionError_(str(e)) from None
    return Credentials(
        tenant_id=tenant.azure_tenant_id,
        client_id=tenant.client_id,
        client_secret=secret,
    )


def probe_powerbi_admin(creds: Credentials) -> None:
    """Base tier: read-only admin APIs. Without this there is no inventory."""
    FabricClient(creds).get_json(
        f"{API_POWERBI}/admin/groups", SCOPE_POWERBI, params={"$top": 1}
    )


ADMIN_API_REQUIRED = (
    "Scans need the read-only admin APIs. In the Fabric admin portal, turn on "
    "'Service principals can use read-only admin APIs' for a security group that "
    "contains this service principal, wait a few minutes, then Re-validate in "
    "Settings → Connection."
)


def admin_api_problem(tenant: ConnectedTenant) -> str | None:
    """
    Why a scan may not start, or None. The read-only admin APIs are mandatory:
    without them Report Audience has nothing and the Data Catalog is half a
    product (no access, labels, or report bindings). Checked live, so a setting
    switched on a minute ago counts without a Re-validate.
    """
    if tenant.is_demo:
        return None
    try:
        probe_powerbi_admin(credentials_for(tenant))
    except AccessDenied as e:
        return f"{ADMIN_API_REQUIRED} ({str(e)[:300]})"
    except Exception:  # noqa: BLE001 — a network blip is the scan's to report, not a block
        return None
    return None


def probe_fabric_api(creds: Credentials) -> None:
    """Fabric REST: item enumeration and, at Contributor, code."""
    items_api.list_workspaces(FabricClient(creds))


def probe_onelake(creds: Credentials) -> None:
    """A token for the storage audience: the Delta-log collector's precondition."""
    FabricClient(creds).tokens.token(SCOPE_STORAGE)


def probe_graph(creds: Credentials) -> None:
    """Microsoft Graph (Audience): licences and Entra group members."""
    from ..audience.powerbi import graph

    graph.probe(creds)


def check_capabilities(creds: Credentials) -> tuple[bool, dict]:
    """
    Probe each credential tier. Returns (base tier usable, capability map).

    Only the Power BI admin tier is required — everything else degrades to a
    labelled gap rather than a failure, which is the whole access model. Each
    probe is its own function so tests can exercise them one at a time.
    """
    from ..audience.powerbi.graph import GraphUnavailable

    caps: dict[str, dict] = {}
    probes = (
        (CAP_POWERBI_ADMIN, probe_powerbi_admin, ""),
        (CAP_FABRIC_API, probe_fabric_api, ""),
        (
            CAP_ONELAKE,
            probe_onelake,
            "OneLake refused the principal. Give it Contributor on the workspace "
            "(Workspace → Manage access). ",
        ),
        (CAP_GRAPH, probe_graph, ""),
    )
    for cap, probe, hint in probes:
        try:
            probe(creds)
            caps[cap] = {"available": True}
        except (AccessDenied, FabricError, GraphUnavailable) as e:
            caps[cap] = {
                "available": False,
                "reason": scrub(f"{hint}{str(e)[:300]}", creds.client_secret)[:400],
            }
    return caps[CAP_POWERBI_ADMIN]["available"], caps


def connect(
    session: Session,
    customer_id: str,
    *,
    azure_tenant_id: str,
    client_id: str,
    client_secret: str,
    display_name: str = "",
) -> ConnectedTenant:
    """Store a connection, validating it first. The secret goes to the store, never the DB."""
    creds = Credentials(
        tenant_id=azure_tenant_id, client_id=client_id, client_secret=client_secret
    )
    ok, caps = check_capabilities(creds)

    store = secrets.default_store()
    existing = repository.get_tenant_by_azure_id(session, customer_id, azure_tenant_id)
    # Reuse the existing key so reconnecting does not orphan a secret in the store.
    key = (
        existing.secret_ref.split(":", 1)[1]
        if existing and ":" in existing.secret_ref
        else ""
    )
    key = store.put(key, client_secret)

    return repository.upsert_tenant(
        session,
        customer_id,
        display_name=display_name or azure_tenant_id,
        azure_tenant_id=azure_tenant_id,
        client_id=client_id,
        secret_ref=f"{store.name}:{key}",
        status=STATUS_CONNECTED if ok else STATUS_ERROR,
        capabilities=caps,
        validation_error=None if ok else caps[CAP_POWERBI_ADMIN].get("reason"),
    )


def bootstrap_from_env(session: Session) -> ConnectedTenant | None:
    """
    Connect the tenant named by FL_TENANT_ID/CLIENT_ID/CLIENT_SECRET at startup.

    Convenience for local development only, and deliberately quiet about
    failure: a machine without those vars set is the normal case, not an error.
    """
    import os

    if not config.BOOTSTRAP_FROM_ENV:
        return None
    tenant_id = os.environ.get("FL_TENANT_ID")
    client_id = os.environ.get("FL_CLIENT_ID")
    client_secret = os.environ.get("FL_CLIENT_SECRET")
    if not all([tenant_id, client_id, client_secret]):
        return None
    if repository.get_tenant_by_azure_id(session, config.DEFAULT_CUSTOMER_ID, tenant_id):
        return None
    try:
        tenant = connect(
            session,
            config.DEFAULT_CUSTOMER_ID,
            azure_tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            display_name=os.environ.get("FL_TENANT_NAME", "Bootstrapped tenant"),
        )
    except Exception as e:  # noqa: BLE001 — startup must never be fatal here
        log.warning("Env bootstrap failed: %s", type(e).__name__)
        return None
    log.info("Bootstrapped tenant %s from the environment.", tenant.display_name)
    return tenant
