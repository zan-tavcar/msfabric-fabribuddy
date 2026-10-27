"""
Activity as rows, fetched incrementally.

The old design stored a 30-day JSON blob per scan: every refresh re-read a
million events over ~9 minutes to learn about one new day. Here events are
rows, so a refresh asks "what is the newest event we hold?" and fetches only
from that day forward — seconds instead of minutes on a warm tenant.

The overlap is deliberate: the newest stored day is re-read in full, because
events for a day keep arriving after midnight. The audit log's own record id
makes that idempotent.
"""

import datetime as dt
import logging

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..fabric.credentials import Credentials
from ..models import ConnectedTenant
from .models import ActivityEvent
from .powerbi import scanner
from .powerbi.scanner import VIEW_ACTION

log = logging.getLogger("audience.activity")

# The audit log retains ~30 days; nothing older is worth keeping.
RETENTION_DAYS = 30


def newest_event(session: Session, tenant_id: str) -> dt.datetime | None:
    return session.scalar(
        select(func.max(ActivityEvent.ts)).where(ActivityEvent.tenant_id == tenant_id)
    )


def _upsert(session: Session, rows: list[dict]) -> int:
    """
    Insert, ignoring events we already hold.

    ON CONFLICT DO NOTHING is the whole point of the unique key: re-reading an
    overlapping day must not double-count. SQLite and Postgres both support it;
    the dialect import is the only thing that differs.
    """
    if not rows:
        return 0
    written = 0
    # Chunked so a 30-day backfill does not build one enormous statement.
    for i in range(0, len(rows), 1000):
        chunk = rows[i : i + 1000]
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(ActivityEvent).values(chunk)
        else:
            stmt = sqlite_insert(ActivityEvent).values(chunk)
        stmt = stmt.on_conflict_do_nothing(index_elements=["tenant_id", "event_key"])
        result = session.execute(stmt)
        written += result.rowcount or 0
    session.commit()
    return written


def prune(session: Session, tenant_id: str) -> int:
    """Drop anything past retention so the table cannot grow without bound."""
    cutoff = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(
        days=RETENTION_DAYS + 2
    )
    result = session.execute(
        delete(ActivityEvent).where(
            ActivityEvent.tenant_id == tenant_id, ActivityEvent.ts < cutoff
        )
    )
    session.commit()
    return result.rowcount or 0


def refresh(
    session: Session,
    tenant: ConnectedTenant,
    creds: Credentials,
    days: int = RETENTION_DAYS,
    ws_by_item: dict | None = None,
    full: bool = False,
) -> dict:
    """
    Bring stored activity up to date.

    Fetches only the days not already held unless `full` is set. Returns what
    it did, so the UI can say "3 new days" rather than implying a full rescan.
    """
    newest = None if full else newest_event(session, tenant.id)
    today = dt.datetime.now(dt.UTC).date()

    if newest is None:
        start_day = today - dt.timedelta(days=days - 1)
        mode = "full"
    else:
        # Re-read the newest stored day: events keep landing after midnight.
        start_day = newest.date()
        mode = "incremental"

    day_count = (today - start_day).days + 1
    day_count = max(1, min(day_count, days))

    log.info(
        "activity %s refresh for tenant %s: %d day(s) from %s",
        mode,
        tenant.id,
        day_count,
        start_day,
    )

    raw = scanner.fetch_activity_rows(
        creds,
        start_day=start_day,
        end_day=today,
        ws_by_item=ws_by_item or {},
    )

    rows = [
        {
            "customer_id": tenant.customer_id,
            "tenant_id": tenant.id,
            "ts": r["ts"],
            "event_key": r["event_key"],
            "user_upn": r["user"][:320],
            "action": r["action"][:80],
            "item_name": r["item"][:400],
            "workspace_name": r["workspace"][:400],
        }
        for r in raw["rows"]
    ]
    written = _upsert(session, rows)
    pruned = prune(session, tenant.id)

    return {
        "mode": mode,
        "daysFetched": day_count,
        "eventsSeen": len(rows),
        "eventsNew": written,
        "eventsPruned": pruned,
        "failedDays": raw["failed_days"],
        "partial": bool(raw["failed_days"]),
    }


def in_scope(scope: dict):
    """
    The events that belong to the scanned workspaces.

    The audit log leaves WorkspaceName empty on most events, so an event counts
    when its workspace is one of the scope's, or — with no workspace — when the
    item it names is a report or model inside the scope.
    """
    from sqlalchemy import and_, or_

    return or_(
        ActivityEvent.workspace_name.in_(scope["workspaces"] or [""]),
        and_(
            ActivityEvent.workspace_name == "",
            ActivityEvent.item_name.in_(scope["items"] or [""]),
        ),
    )


def aggregate(
    session: Session,
    tenant: ConnectedTenant,
    days: int = RETENTION_DAYS,
    scope: dict | None = None,
) -> dict:
    """
    Build the payload the UI consumes, entirely in SQL.

    Doing this per request rather than at scan time means changing the window
    is instant and never re-reads the audit log.

    `scope` ({"workspaces": names, "items": names}) narrows everything to the
    scanned workspaces, so Activity describes the same estate as the Overview.
    The log itself stays stored tenant-wide: the unused-seat check reads it
    unscoped, because a person active only elsewhere is not an unused seat.
    """
    since = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=days)
    window = (ActivityEvent.tenant_id == tenant.id, ActivityEvent.ts >= since)
    stored = (
        session.scalar(select(func.count()).select_from(ActivityEvent).where(*window)) or 0
    )
    if stored == 0:
        return {"live": False, "scanned": False, "error": "No activity stored yet."}
    base = (*window, in_scope(scope)) if scope is not None else window
    views_only = (*base, ActivityEvent.action == VIEW_ACTION)

    total = (
        session.scalar(select(func.count()).select_from(ActivityEvent).where(*base)) or 0
    )
    views_elsewhere = 0
    if scope is not None:
        all_views = (
            session.scalar(
                select(func.count())
                .select_from(ActivityEvent)
                .where(*window, ActivityEvent.action == VIEW_ACTION)
            )
            or 0
        )

    views = (
        session.scalar(select(func.count()).select_from(ActivityEvent).where(*views_only))
        or 0
    )
    if scope is not None:
        views_elsewhere = max(0, all_views - views)
    people = (
        session.scalar(
            select(func.count(func.distinct(ActivityEvent.user_upn))).where(*base)
        )
        or 0
    )
    viewers = (
        session.scalar(
            select(func.count(func.distinct(ActivityEvent.user_upn))).where(*views_only)
        )
        or 0
    )

    day = func.substr(
        func.replace(
            func.cast(ActivityEvent.ts, __import__("sqlalchemy").String), " ", "T"
        ),
        1,
        10,
    )

    daily_rows = session.execute(
        select(
            day.label("d"),
            func.count().label("n"),
            func.count(func.distinct(ActivityEvent.user_upn)).label("u"),
        )
        .where(*views_only)
        .group_by("d")
    ).all()
    daily = {r.d: {"views": r.n, "viewers": r.u} for r in daily_rows}

    top_reports = session.execute(
        select(ActivityEvent.item_name, func.count().label("n"))
        .where(*views_only, ActivityEvent.item_name != "")
        .group_by(ActivityEvent.item_name)
        .order_by(func.count().desc())
        .limit(15)
    ).all()

    top_users = session.execute(
        select(ActivityEvent.user_upn, func.count().label("n"))
        .where(*views_only, ActivityEvent.user_upn != "")
        .group_by(ActivityEvent.user_upn)
        .order_by(func.count().desc())
        .limit(15)
    ).all()

    last_by_user = dict(
        session.execute(
            select(ActivityEvent.user_upn, func.max(ActivityEvent.ts))
            .where(*base, ActivityEvent.user_upn != "")
            .group_by(ActivityEvent.user_upn)
        ).all()
    )
    last_by_item = dict(
        session.execute(
            select(ActivityEvent.item_name, func.max(ActivityEvent.ts))
            .where(*base, ActivityEvent.item_name != "")
            .group_by(ActivityEvent.item_name)
        ).all()
    )
    last_view_by_item = dict(
        session.execute(
            select(ActivityEvent.item_name, func.max(ActivityEvent.ts))
            .where(*views_only, ActivityEvent.item_name != "")
            .group_by(ActivityEvent.item_name)
        ).all()
    )
    last_by_ws = dict(
        session.execute(
            select(ActivityEvent.workspace_name, func.max(ActivityEvent.ts))
            .where(*base, ActivityEvent.workspace_name != "")
            .group_by(ActivityEvent.workspace_name)
        ).all()
    )
    events_by_ws = dict(
        session.execute(
            select(ActivityEvent.workspace_name, func.count())
            .where(*base, ActivityEvent.workspace_name != "")
            .group_by(ActivityEvent.workspace_name)
        ).all()
    )
    last_refresh = dict(
        session.execute(
            select(ActivityEvent.item_name, func.max(ActivityEvent.ts))
            .where(
                *base, ActivityEvent.action.like("%Refresh%"), ActivityEvent.item_name != ""
            )
            .group_by(ActivityEvent.item_name)
        ).all()
    )
    action_mix = session.execute(
        select(ActivityEvent.action, func.count())
        .where(*base)
        .group_by(ActivityEvent.action)
        .order_by(func.count().desc())
    ).all()

    # The log ships every view; there are thousands, not a million.
    view_events = (
        session.execute(
            select(ActivityEvent)
            .where(*views_only)
            .order_by(ActivityEvent.ts.desc())
            .limit(20000)
        )
        .scalars()
        .all()
    )

    iso = lambda d: d.isoformat() if hasattr(d, "isoformat") else str(d)  # noqa: E731
    covered = sorted(daily)

    return {
        "live": True,
        "scanned": True,
        "events": [
            {
                "ts": iso(e.ts),
                "user": e.user_upn,
                "action": e.action,
                "report": e.item_name,
                "ws": e.workspace_name,
            }
            for e in view_events
        ],
        "summary": {
            "total": total,
            "views": views,
            "distinctUsers": people,
            "viewerUsers": viewers,
            "days": days,
            "daysRequested": days,
            "daysWithData": len(covered),
            "partial": False,
            "storedEvents": total,
        },
        # What the figures cover: the scanned workspaces, or the whole tenant.
        "scope": {
            "scoped": scope is not None,
            "workspaces": sorted(scope["workspaces"]) if scope is not None else [],
            "viewsElsewhere": views_elsewhere,
        },
        "topReports": [{"report": r[0], "views": r[1]} for r in top_reports],
        "topUsers": [{"user": r[0], "views": r[1]} for r in top_users],
        "actionMix": [{"action": r[0], "count": r[1]} for r in action_mix],
        "activityTypes": sorted({r[0] for r in action_mix if r[0]}),
        "daily": _fill_days(daily, days),
        "lastSeenByUser": {k: iso(v) for k, v in last_by_user.items()},
        "lastSeenByItem": {k: iso(v) for k, v in last_by_item.items()},
        "lastSeenByReport": {k: iso(v) for k, v in last_view_by_item.items()},
        "lastSeenByWorkspace": {k: iso(v) for k, v in last_by_ws.items()},
        "eventsByWorkspace": events_by_ws,
        "lastRefreshByDataset": {k: iso(v) for k, v in last_refresh.items()},
        "activeUsers": sorted(last_by_user),
        "touchedReports": sorted(last_view_by_item),
    }


def _fill_days(daily: dict, days: int) -> list[dict]:
    """Every day in the window, so a gap reads as silence rather than absence."""
    today = dt.datetime.now(dt.UTC).date()
    out = []
    for i in range(days - 1, -1, -1):
        key = (today - dt.timedelta(days=i)).isoformat()
        d = daily.get(key, {"views": 0, "viewers": 0})
        out.append(
            {
                "date": key,
                "views": d["views"],
                "viewers": d["viewers"],
                "other": 0,
                "users": d["viewers"],
            }
        )
    return out
