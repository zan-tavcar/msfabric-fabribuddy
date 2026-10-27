"""
The public demo: app.fabribuddy.com/demo, no account needed (FL_DEMO_ORG=1).

A read-only organisation, "Contoso (demo)", lives in the production database
beside the customers. Its Data Catalog is the built-in scenario (the real
parsers over app/lineage/demo_data); its Report Audience is what the real
scanners stored from an invented tenant (scripts/demo/build_audience_demo.py →
app/audience/demo_data.json.gz). Rebuilt at startup and once a day, so every
"last 30 days" and "last write" is current.

Visitors are signed in as one shared **viewer**: the role already refuses
scans, settings, members and descriptions; `is_demo_user` covers the few things
a viewer may change about themselves (profile, password). The organisation is
flagged like the operator's (never capped, locked or purged) and is left out
of the console's customer figures.
"""

import datetime as dt
import gzip
import json
import logging
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import config, entitlements
from .models import Customer, UserAccount, utcnow

log = logging.getLogger("fabribuddy.demo")

CUSTOMER_ID = "fabribuddy-demo"
EMAIL = "demo@fabribuddy.com"
ORG_NAME = "Contoso (demo)"
TENANT_NAME = "Contoso"
AUDIENCE_DATA = Path(__file__).parent / "audience" / "demo_data.json.gz"
REFRESH_HOURS = 24

_last_refresh: dt.datetime | None = None


def is_demo_user(user: UserAccount | None) -> bool:
    return bool(user) and user.email == EMAIL


def is_demo_customer(customer_id: str | None) -> bool:
    return customer_id == CUSTOMER_ID


def demo_user(session: Session) -> UserAccount | None:
    return session.scalars(select(UserAccount).where(UserAccount.email == EMAIL)).first()


def ensure(session: Session) -> None:
    """Create or rebuild the demo organisation. Never raises: the app starts regardless."""
    global _last_refresh
    try:
        _build(session)
        _last_refresh = utcnow()
    except Exception:  # noqa: BLE001 — the demo must never take the product down
        session.rollback()
        log.exception("could not build the demo organisation")


def refresh_if_stale(session: Session) -> None:
    """From the scheduler: rebuild once a day, so the dates keep moving."""
    if not config.DEMO_ORG:
        return
    if _last_refresh is None or utcnow() - _last_refresh > dt.timedelta(
        hours=REFRESH_HOURS
    ):
        ensure(session)


def _build(session: Session) -> None:
    from .lineage import demo
    from .lineage import repository as lineage_repository

    customer = session.get(Customer, CUSTOMER_ID)
    if customer is None:
        customer = entitlements.open_organisation(session, CUSTOMER_ID, ORG_NAME)
    customer.name = ORG_NAME
    customer.is_platform_admin = 1  # never capped, locked or purged
    if demo_user(session) is None:
        session.add(
            UserAccount(
                email=EMAIL,
                display_name="Demo visitor",
                password_hash="",  # nobody signs in with a password
                customer_id=CUSTOMER_ID,
                role="viewer",
                email_verified=1,
            )
        )
    session.commit()

    tenant = demo.ensure_tenant(session, CUSTOMER_ID, TENANT_NAME)
    lineage_repository.delete_tenant_data(session, CUSTOMER_ID, tenant.id)
    session.commit()
    demo.seed(session, tenant)
    _scan_record(session, tenant)
    _audience(session, tenant)
    log.info("demo organisation rebuilt")


def _scan_record(session: Session, tenant) -> None:
    """One successful run per Data Catalog step, so "Last scan" reads as it would."""
    from .models import CollectorRun
    from .services.lineage import STEP_NAMES

    now = utcnow()
    for kind in STEP_NAMES:
        session.add(
            CollectorRun(
                tenant_id=tenant.id,
                customer_id=CUSTOMER_ID,
                kind=kind,
                status="ok",
                params={},
                stats={},
                started_at=now - dt.timedelta(minutes=4),
                finished_at=now,
            )
        )
    session.commit()


def _audience(session: Session, tenant) -> None:
    """Load the stored Report Audience scans, activity shifted to end today."""
    from .audience.models import ActivityEvent, ScanRun

    data = json.loads(gzip.decompress(AUDIENCE_DATA.read_bytes()))
    shift = utcnow() - dt.datetime.fromisoformat(data["capturedAt"])
    now = utcnow()
    session.execute(delete(ScanRun).where(ScanRun.tenant_id == tenant.id))
    session.execute(delete(ActivityEvent).where(ActivityEvent.tenant_id == tenant.id))
    for kind, run in data["runs"].items():
        session.add(
            ScanRun(
                tenant_id=tenant.id,
                customer_id=CUSTOMER_ID,
                kind=kind,
                status="ok",
                params=run["params"],
                payload=run["payload"],
                started_at=now,
                finished_at=now,
            )
        )
    for ts, key, upn, action, item, workspace in data["events"]:
        session.add(
            ActivityEvent(
                customer_id=CUSTOMER_ID,
                tenant_id=tenant.id,
                ts=dt.datetime.fromisoformat(ts) + shift,
                event_key=key,
                user_upn=upn,
                action=action,
                item_name=item,
                workspace_name=workspace,
            )
        )
    tenant.capabilities = {
        **(tenant.capabilities or {}),
        "graph_licenses": {"available": True},
    }
    session.commit()
