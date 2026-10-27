"""
A scan pass: every product the organisation has, one after the other.

What Rescan, Admin → Scan now and the schedule run. Lineage first (its
inventory is the slower, rate-limited one), then Audience. A product whose
subscription has ended is skipped; one product failing never stops the other.
"""

import logging
import time

from sqlalchemy.orm import Session

from .. import entitlements, plans
from ..models import TRIGGER_MANUAL, ConnectedTenant, ScanPass, utcnow
from . import progress

log = logging.getLogger("fabribuddy.pass")


def _steps(tenant: ConnectedTenant, products: list[str]) -> list[tuple[str, str]]:
    from ..audience import scans as audience_scans
    from . import lineage as lineage_service

    out: list[tuple[str, str]] = []
    for product in products:
        names = (
            lineage_service.STEP_NAMES
            if product == plans.LINEAGE
            else audience_scans.step_names(tenant)
        )
        out.extend((product, name) for name in names)
    return out


def _failed(result) -> bool:
    """A step's result: Lineage returns stats or {"error": …}, Audience "ok" or a message."""
    if isinstance(result, dict):
        return "error" in result
    return result != "ok"


def run_pass(
    session: Session,
    tenant: ConnectedTenant,
    *,
    incremental: bool = True,
    products: list[str] | None = None,
    trigger: str = TRIGGER_MANUAL,
) -> dict:
    from ..audience import scans as audience_scans
    from . import lineage as lineage_service

    wanted = products or list(plans.PRODUCTS)
    active = entitlements.active_products(session, tenant.customer_id)
    runnable = [p for p in wanted if p in active]
    steps = _steps(tenant, runnable)

    record = ScanPass(
        tenant_id=tenant.id,
        customer_id=tenant.customer_id,
        trigger=trigger,
        incremental=1 if incremental else 0,
        products=runnable,
        steps_total=len(steps),
        outcome={},
    )
    session.add(record)
    session.commit()
    progress.begin(tenant.id, record.id, steps, record.started_at.isoformat())

    # Per product and step: status and seconds taken, for the history.
    timings: dict[str, dict[str, dict]] = {}
    clock = {"step": None, "at": time.monotonic(), "done": 0}

    def close_step() -> None:
        if clock["step"] is None:
            return
        product, name = clock["step"]
        timings.setdefault(product, {})[name] = {
            "seconds": round(time.monotonic() - clock["at"], 1)
        }
        clock["done"] += 1

    def on_step_for(product: str):
        def on_step(name: str) -> None:
            close_step()
            clock["step"], clock["at"] = (product, name), time.monotonic()
            progress.step(tenant.id, product, name, clock["done"])
            record.steps_done = clock["done"]
            session.commit()

        return on_step

    outcome: dict = {}
    # The read-only admin APIs are mandatory: a scheduled pass without them
    # is recorded as failed with the fix, rather than half-running.
    from . import connections

    problem = connections.admin_api_problem(tenant)
    if problem:
        log.warning("pass refused for tenant %s: admin APIs denied", tenant.id)
        outcome = {p: f"failed: {problem}" for p in runnable}
        progress.end(tenant.id)
        _finish(session, record, outcome, timings)
        return outcome
    try:
        for product in wanted:
            if product not in active:
                outcome[product] = "skipped: subscription ended"
                continue
            try:
                if product == plans.LINEAGE:
                    outcome[product] = lineage_service.run_full_refresh(
                        session,
                        tenant,
                        incremental=incremental,
                        on_step=on_step_for(product),
                    )
                else:
                    outcome[product] = audience_scans.run_full_refresh(
                        session, tenant, on_step=on_step_for(product)
                    )
            except Exception as e:  # noqa: BLE001 — one product must not stop the other
                log.exception("%s pass failed for tenant %s", product, tenant.id)
                outcome[product] = f"failed: {type(e).__name__}"
                session.rollback()
            close_step()
            clock["step"] = None
    finally:
        progress.end(tenant.id)
        _finish(session, record, outcome, timings)
    return outcome


def _finish(session: Session, record: ScanPass, outcome: dict, timings: dict) -> None:
    """Close the pass row: status from what each step returned."""
    failed = done = 0
    summary: dict = {}
    for product, result in outcome.items():
        steps = timings.get(product, {})
        if isinstance(result, dict):
            for name, value in result.items():
                bad = _failed(value)
                failed += bad
                done += 1
                steps.setdefault(name, {})["status"] = "failed" if bad else "ok"
                if bad:
                    message = value.get("error") if isinstance(value, dict) else value
                    steps[name]["error"] = str(message)[:300]
        else:
            # Skipped, or crashed before its steps could report.
            summary[product] = {"status": str(result)[:200]}
            failed += 0 if str(result).startswith("skipped") else 1
            continue
        summary[product] = {"steps": steps}
    record.outcome = summary
    record.steps_done = done
    record.steps_failed = failed
    record.finished_at = utcnow()
    if failed == 0:
        record.status = "ok"
    elif done and failed < done:
        record.status = "partial"
    else:
        record.status = "failed"
    try:
        session.commit()
    except Exception:  # noqa: BLE001 — the history must not mask the scan's own result
        log.exception("could not record scan pass %s", record.id)
        session.rollback()


def succeeded(outcome: dict) -> bool:
    """Every product ran and every step inside it succeeded."""
    for result in outcome.values():
        if not isinstance(result, dict):
            return False  # skipped or failed outright
        for step in result.values():
            if step != "ok" and not (isinstance(step, dict) and "error" not in step):
                return False
    return True
