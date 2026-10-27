"""
Jobs collector: run history for the items that write data.

Collection only. Each Fabric job instance becomes a `job_run` row; correlating
a run with the Delta commits it produced (`delta_version.job_run_id`) is the
Phase 6 step and is not attempted here — a wrong correlation would send someone
to the wrong notebook, and an absent one only leaves a field empty.
"""

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...fabric import items as items_api
from ...fabric.client import AccessDenied, FabricClient, FabricError
from .. import repository
from ..models import JobRun

log = logging.getLogger("lineage.collect.jobs")

# Item types whose runs write data. Reports and models have no job history
# worth showing here (a model refresh is a different API).
JOB_ITEM_TYPES = {"Notebook", "DataPipeline", "Dataflow", "SparkJobDefinition", "CopyJob"}


def _parse_ts(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.astimezone(dt.UTC).replace(tzinfo=None) if value.tzinfo else value


def collect(
    session: Session,
    client: FabricClient,
    *,
    customer_id: str,
    tenant_id: str,
    contributor_workspaces: set[str],
) -> dict:
    """Upsert the job history of every runnable item in an in-scope workspace."""
    contributor_workspaces = {w.lower() for w in contributor_workspaces}
    errors: list[dict] = []
    counts = {"items": 0, "runs": 0, "new": 0}

    workspaces = {
        w.id: w for w in repository.list_workspaces(session, customer_id, tenant_id)
    }
    items = [
        i
        for i in repository.list_items(session, customer_id, tenant_id)
        if i.item_type in JOB_ITEM_TYPES
    ]

    for item in items:
        workspace = workspaces.get(item.workspace_id)
        if (
            workspace is None
            or workspace.workspace_guid.lower() not in contributor_workspaces
        ):
            continue
        try:
            instances = items_api.list_job_instances(
                client, workspace.workspace_guid, item.item_guid
            )
        except (AccessDenied, FabricError) as e:
            errors.append(
                {
                    "scope": "item",
                    "scope_id": item.id,
                    "scope_name": f"{workspace.name} / {item.display_name}",
                    "reason": "needs_contributor"
                    if isinstance(e, AccessDenied)
                    else "error",
                    "message": str(e)[:500],
                }
            )
            continue

        counts["items"] += 1
        existing = {
            r.run_guid: r
            for r in session.scalars(select(JobRun).where(JobRun.item_id == item.id))
        }
        for inst in instances:
            run_guid = (inst.get("id") or "").lower()
            if not run_guid:
                continue
            row = existing.get(run_guid)
            if row is None:
                row = JobRun(
                    customer_id=customer_id,
                    tenant_id=tenant_id,
                    item_id=item.id,
                    run_guid=run_guid,
                )
                session.add(row)
                existing[run_guid] = row
                counts["new"] += 1
            row.job_type = inst.get("jobType") or ""
            row.status = inst.get("status") or ""
            row.started_at = _parse_ts(inst.get("startTimeUtc"))
            row.ended_at = _parse_ts(inst.get("endTimeUtc"))
            row.parameters = {
                k: inst[k]
                for k in ("invokeType", "rootActivityId", "failureReason")
                if inst.get(k)
            }
            counts["runs"] += 1
        session.commit()

    return {"stats": counts, "errors": errors}
