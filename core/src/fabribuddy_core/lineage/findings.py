"""
The findings engine.

Each rule answers a question a customer would actually ask, and each finding
carries enough detail to act on. Rules deliberately refuse to fire when they
lack the evidence: a "stale table" finding on a table whose Delta log we could
not read would be a guess presented as a fact, and one wrong finding costs more
trust than ten missing ones.

Findings are upserted on (type, node) and *resolved* when they stop applying, so
the list reflects the current state rather than accumulating history.
"""

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import utcnow
from . import repository
from .models import (
    KIND_LAKEHOUSE_TABLE,
    KIND_WAREHOUSE_TABLE,
    STORAGE_KINDS,
    AccessGrant,
    ColumnNode,
    DeltaStats,
    Finding,
)

log = logging.getLogger("lineage.findings")

TYPE_STALE = "stale"
TYPE_PII = "pii_exposure"
TYPE_UNDOCUMENTED = "undocumented"
TYPE_INDIRECT = "indirect_access"
TYPE_ORPHAN = "orphan"
TYPE_UNRESOLVED = "unresolved_lineage"

SEV_CRITICAL = "critical"
SEV_SERIOUS = "serious"
SEV_WARNING = "warning"

# Column-name heuristics for PII. Word-boundary matched, because a substring
# rule flags `email_campaign_id` and `name_id` and the noise trains people to
# ignore the whole feature.
PII_PATTERNS = [
    (r"\b(e?mail|email_address)\b", "email"),
    (r"\b(phone|mobile|telephone|msisdn)\b", "phone"),
    (r"\b(iban|bic|swift|account_number|card_number|creditcard)\b", "financial"),
    (r"\b(address|street|postcode|zip_?code|city_line)\b", "address"),
    (r"\b(birth_?date|dob|date_of_birth)\b", "birth date"),
    (r"\b(ssn|nino|national_?id|tax_?id|vat_?number|personal_?id)\b", "national id"),
    (r"\b(passport|driver_?licen[cs]e)\b", "identity document"),
    (r"\b(first_?name|last_?name|sur_?name|full_?name|customer_?name)\b", "name"),
    (r"\b(latitude|longitude|geo_?location)\b", "location"),
]
_COMPILED_PII = [(re.compile(p, re.IGNORECASE), label) for p, label in PII_PATTERNS]

# A table is undocumented when this share of its columns have no description.
UNDOCUMENTED_THRESHOLD = 0.5

# How long after its expected cadence a table counts as stale.
STALE_GRACE_HOURS = 6


def classify_pii(column_name: str) -> str:
    """The PII category a column name suggests, or "" for none."""
    for pattern, label in _COMPILED_PII:
        if pattern.search(column_name or ""):
            return label
    return ""


def tag_pii_columns(session: Session, customer_id: str, tenant_id: str) -> int:
    """
    Flag columns whose names look like PII.

    Heuristic, and honest about it: the tag reads "PII (auto)" in the UI so
    nobody mistakes a guess for a classification. A human tag always wins.
    """
    stmt = select(ColumnNode).where(
        ColumnNode.customer_id == customer_id, ColumnNode.tenant_id == tenant_id
    )
    tagged = 0
    for column in session.scalars(stmt):
        category = classify_pii(column.name)
        existing = list(column.tags or [])
        has_manual = any(t.upper() == "PII" for t in existing)
        auto_tag = "PII (auto)"
        if category and not has_manual and auto_tag not in existing:
            column.tags = [*existing, auto_tag]
            column.is_pii = True
            tagged += 1
        elif category and has_manual:
            column.is_pii = True
        elif not category and auto_tag in existing:
            # The heuristic changed its mind; remove the tag it added rather
            # than leaving a stale claim.
            column.tags = [t for t in existing if t != auto_tag]
            column.is_pii = has_manual
    session.commit()
    return tagged


def _upsert(
    session: Session,
    customer_id: str,
    tenant_id: str,
    *,
    finding_type: str,
    severity: str,
    node_id: str | None,
    item_id: str | None,
    message: str,
    details: dict,
    score: float = 0.0,
) -> None:
    stmt = select(Finding).where(
        Finding.tenant_id == tenant_id,
        Finding.finding_type == finding_type,
        Finding.node_id.is_(node_id) if node_id is None else Finding.node_id == node_id,
        Finding.item_id.is_(item_id) if item_id is None else Finding.item_id == item_id,
    )
    row = session.scalars(stmt).first()
    if row is None:
        row = Finding(
            customer_id=customer_id,
            tenant_id=tenant_id,
            finding_type=finding_type,
            node_id=node_id,
            item_id=item_id,
        )
        session.add(row)
    row.severity = severity
    row.message = message
    row.details = details
    row.score = score
    row.detected_at = utcnow()
    row.resolved_at = None


def run_all(session: Session, customer_id: str, tenant_id: str) -> dict:
    """
    Evaluate every rule. Returns counts per finding type.

    Findings that no longer apply are resolved rather than deleted, so the list
    shows the current state without losing that something was once true.
    """
    tag_pii_columns(session, customer_id, tenant_id)

    nodes = repository.list_nodes(session, customer_id, tenant_id)
    items = {i.id: i for i in repository.list_items(session, customer_id, tenant_id)}
    stats = {
        row.node_id: row
        for row in session.scalars(
            select(DeltaStats).where(
                DeltaStats.customer_id == customer_id,
                DeltaStats.tenant_id == tenant_id,
            )
        )
    }
    columns_by_node: dict[str, list[ColumnNode]] = {}
    for column in session.scalars(
        select(ColumnNode).where(
            ColumnNode.customer_id == customer_id, ColumnNode.tenant_id == tenant_id
        )
    ):
        columns_by_node.setdefault(column.node_id, []).append(column)

    live: set[tuple[str, str | None]] = set()
    counts = dict.fromkeys(
        (
            TYPE_STALE,
            TYPE_PII,
            TYPE_UNDOCUMENTED,
            TYPE_INDIRECT,
            TYPE_ORPHAN,
            TYPE_UNRESOLVED,
        ),
        0,
    )

    for rule in (
        _rule_stale,
        _rule_pii_exposure,
        _rule_undocumented,
        _rule_indirect_access,
        _rule_orphan,
        _rule_unresolved,
    ):
        found = rule(
            session,
            customer_id,
            tenant_id,
            nodes=nodes,
            items=items,
            stats=stats,
            columns_by_node=columns_by_node,
            live=live,
        )
        for finding_type, n in found.items():
            counts[finding_type] = counts.get(finding_type, 0) + n

    _resolve_stale_findings(session, customer_id, tenant_id, live)
    session.commit()
    return counts


def _resolve_stale_findings(
    session: Session, customer_id: str, tenant_id: str, live: set
) -> None:
    stmt = select(Finding).where(
        Finding.customer_id == customer_id,
        Finding.tenant_id == tenant_id,
        Finding.resolved_at.is_(None),
    )
    for row in session.scalars(stmt):
        if (row.finding_type, row.node_id) not in live:
            row.resolved_at = utcnow()


# --- rules -----------------------------------------------------------------
def _rule_stale(session, customer_id, tenant_id, *, nodes, stats, live, **_):
    """
    A table whose last write is older than its cadence suggests.

    The cadence comes from the *observed* interval between the last few writes,
    not from a configured schedule — a customer's schedule lives in Fabric and we
    would only be guessing at it. A table with fewer than three writes on record
    is skipped: there is no cadence to be late against.
    """
    from .models import DeltaVersion

    found = 0
    now = utcnow()
    for node in nodes:
        if node.kind not in STORAGE_KINDS:
            continue
        stat = stats.get(node.id)
        if stat is None or stat.last_write_at is None:
            continue

        timestamps = [
            row[0]
            for row in session.execute(
                select(DeltaVersion.committed_at)
                .where(
                    DeltaVersion.tenant_id == tenant_id,
                    DeltaVersion.node_id == node.id,
                    DeltaVersion.committed_at.isnot(None),
                )
                .order_by(DeltaVersion.committed_at.desc())
                .limit(6)
            )
        ]
        if len(timestamps) < 3:
            continue

        gaps = [
            (timestamps[i] - timestamps[i + 1]).total_seconds()
            for i in range(len(timestamps) - 1)
        ]
        gaps = [g for g in gaps if g > 0]
        if not gaps:
            continue
        typical = sorted(gaps)[len(gaps) // 2]
        overdue_by = (now - stat.last_write_at).total_seconds() - typical
        if overdue_by <= STALE_GRACE_HOURS * 3600:
            continue

        hours_late = overdue_by / 3600
        _upsert(
            session,
            customer_id,
            tenant_id,
            finding_type=TYPE_STALE,
            severity=SEV_SERIOUS if hours_late > 48 else SEV_WARNING,
            node_id=node.id,
            item_id=None,
            message=(
                f"{node.table_name} is stale — last written "
                f"{stat.last_write_at:%d %b %H:%M}, but it usually writes every "
                f"{_humanise(typical)}."
            ),
            details={
                "lastWriteAt": stat.last_write_at.isoformat(),
                "typicalIntervalSeconds": typical,
                "hoursLate": round(hours_late, 1),
            },
            score=hours_late,
        )
        live.add((TYPE_STALE, node.id))
        found += 1
    return {TYPE_STALE: found}


def _rule_pii_exposure(
    session, customer_id, tenant_id, *, nodes, columns_by_node, live, **_
):
    """
    A PII column readable by principals beyond the owning team.

    Fires on the count of non-admin readers, because "email is exposed to 38
    analysts" is actionable and "this table contains PII" is not.
    """
    found = 0
    for node in nodes:
        pii_columns = [c for c in columns_by_node.get(node.id, []) if c.is_pii]
        if not pii_columns:
            continue

        grants = list(
            session.scalars(
                select(AccessGrant).where(
                    AccessGrant.tenant_id == tenant_id,
                    AccessGrant.node_id == node.id,
                )
            )
        )
        if not grants:
            continue
        # Admins and owners are supposed to see everything; the finding is about
        # everyone else.
        readers = {
            g.principal_id for g in grants if g.permission.lower() not in ("admin", "owner")
        }
        if not readers:
            continue

        names = ", ".join(sorted(c.name for c in pii_columns)[:3])
        _upsert(
            session,
            customer_id,
            tenant_id,
            finding_type=TYPE_PII,
            severity=SEV_CRITICAL if len(readers) > 20 else SEV_SERIOUS,
            node_id=node.id,
            item_id=None,
            message=(
                f"PII readable by {len(readers)} principals — "
                f"{node.table_name}.{names} "
                f"{'is' if len(pii_columns) == 1 else 'are'} exposed."
            ),
            details={
                "columns": [c.name for c in pii_columns],
                "readerCount": len(readers),
            },
            score=len(readers),
        )
        live.add((TYPE_PII, node.id))
        found += 1
    return {TYPE_PII: found}


def _rule_undocumented(
    session, customer_id, tenant_id, *, nodes, columns_by_node, live, **_
):
    """A table with no description, or most of its columns undescribed."""
    docs = repository.resolve_docs(session, customer_id, tenant_id, [n.id for n in nodes])
    found = 0
    for node in nodes:
        if node.kind not in STORAGE_KINDS:
            continue
        columns = columns_by_node.get(node.id, [])
        if not columns:
            continue
        described = sum(1 for c in columns if (c.description or "").strip())
        share_undocumented = 1 - (described / len(columns))
        doc = docs.get(node.id)
        table_described = bool(doc and (doc.description or "").strip())

        if table_described and share_undocumented < UNDOCUMENTED_THRESHOLD:
            continue

        _upsert(
            session,
            customer_id,
            tenant_id,
            finding_type=TYPE_UNDOCUMENTED,
            severity=SEV_WARNING,
            node_id=node.id,
            item_id=None,
            message=(
                f"{node.table_name} is undocumented — "
                f"{'no table description' if not table_described else 'table described'}"
                f", {len(columns) - described} of {len(columns)} columns without one."
            ),
            details={
                "columnsTotal": len(columns),
                "columnsDocumented": described,
                "tableDescribed": table_described,
            },
            score=share_undocumented,
        )
        live.add((TYPE_UNDOCUMENTED, node.id))
        found += 1
    return {TYPE_UNDOCUMENTED: found}


def _rule_indirect_access(session, customer_id, tenant_id, *, nodes, live, **_):
    """
    Tables readable through a fixed-identity semantic model.

    The one the portal will not tell you: the model's identity reads the table,
    so table permissions are bypassed and RLS is the only guard.
    """
    found = 0
    for node in nodes:
        grants = list(
            session.scalars(
                select(AccessGrant).where(
                    AccessGrant.tenant_id == tenant_id,
                    AccessGrant.node_id == node.id,
                    AccessGrant.is_indirect.is_(True),
                )
            )
        )
        if not grants:
            continue
        principals = {g.principal_id for g in grants}
        paths = sorted({g.path_description for g in grants})
        _upsert(
            session,
            customer_id,
            tenant_id,
            finding_type=TYPE_INDIRECT,
            severity=SEV_SERIOUS if len(principals) > 50 else SEV_WARNING,
            node_id=node.id,
            item_id=None,
            message=(
                f"{len(principals)} principals read {node.table_name} via a "
                f"fixed-identity semantic model — table permissions are bypassed "
                f"and RLS is the only guard."
            ),
            details={"principalCount": len(principals), "paths": paths},
            score=len(principals),
        )
        live.add((TYPE_INDIRECT, node.id))
        found += 1
    return {TYPE_INDIRECT: found}


def _rule_orphan(session, customer_id, tenant_id, *, nodes, live, **_):
    """
    A table something builds but nothing consumes.

    Only fires when the table has a producer: a table nobody writes and nobody
    reads is a landing zone or a leftover, but one that a notebook or dataflow
    keeps rebuilding for no reader is compute spent on nothing — or lineage we
    failed to capture, which is worth knowing too.
    """
    from .graph import load_adjacency

    adjacency = load_adjacency(session, customer_id, tenant_id)
    found = 0
    for node in nodes:
        if node.kind not in (KIND_LAKEHOUSE_TABLE, KIND_WAREHOUSE_TABLE):
            continue
        if not adjacency.parents.get(node.id) or adjacency.children.get(node.id):
            continue
        _upsert(
            session,
            customer_id,
            tenant_id,
            finding_type=TYPE_ORPHAN,
            severity=SEV_WARNING,
            node_id=node.id,
            item_id=None,
            message=(
                f"{node.table_name} is written but nothing downstream reads it "
                f"— either unused, or its consumers are not captured."
            ),
            details={"layer": node.layer},
            score=1.0,
        )
        live.add((TYPE_ORPHAN, node.id))
        found += 1
    return {TYPE_ORPHAN: found}


def _rule_unresolved(session, customer_id, tenant_id, *, items, live, **_):
    """
    Items whose parse left references unresolved.

    Points at the item, not a table, because the fix is in the code: a
    dynamically built table name needs a human to say what it resolves to.
    """
    from .models import CodeArtifact, ParseResult

    found = 0
    stmt = (
        select(ParseResult, CodeArtifact)
        .join(CodeArtifact, CodeArtifact.id == ParseResult.artifact_id)
        .where(
            ParseResult.customer_id == customer_id,
            ParseResult.tenant_id == tenant_id,
        )
    )
    by_item: dict[str, list[str]] = {}
    for parse_result, artifact in session.execute(stmt):
        warnings = [
            w
            for w in (parse_result.warnings or [])
            if "dynamic" in w.lower() or "could not" in w.lower()
        ]
        if warnings:
            by_item.setdefault(artifact.item_id, []).extend(warnings)

    for item_id, warnings in by_item.items():
        item = items.get(item_id)
        if item is None:
            continue
        _upsert(
            session,
            customer_id,
            tenant_id,
            finding_type=TYPE_UNRESOLVED,
            severity=SEV_WARNING,
            node_id=None,
            item_id=item_id,
            message=(
                f"{item.display_name} has {len(warnings)} unresolved reference"
                f"{'s' if len(warnings) != 1 else ''} — lineage through it is incomplete."
            ),
            details={"warnings": warnings[:10]},
            score=len(warnings),
        )
        live.add((TYPE_UNRESOLVED, None))
        found += 1
    return {TYPE_UNRESOLVED: found}


def _humanise(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 1.5:
        return f"{int(seconds // 60)} minutes"
    if hours < 36:
        return f"{hours:.0f} hours"
    return f"{hours / 24:.0f} days"
