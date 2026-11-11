"""
Jobs collector: run history, schedules, and what each run executed.

For every runnable item in a workspace the principal can read:

- **Runs** — Fabric job instances (notebooks, pipelines, dataflows, Spark job
  definitions, copy jobs) and, for semantic models, the Power BI refresh
  history. One `job_run` row each.
- **Schedules** — the Job Scheduler's schedules (a model's refresh schedule),
  replaced on every scan. The Job Monitor shows them and the next run.
- **Activity runs** — for each pipeline run, the activities it executed (a
  notebook, a child pipeline, a dataflow, a model refresh, a copy) with their
  own status and timings, from `queryactivityruns`. Read once per finished run;
  a run still in progress is read again next time.

Afterwards each child pipeline / notebook run an activity names is linked to
its parent (`job_run.invoked_by_item_id`), so a run of `pl_master` can be
followed down to every run it caused.

Correlating a run with the Delta commits it produced is not attempted here —
a wrong correlation would send someone to the wrong notebook.
"""

import datetime as dt
import json
import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...fabric import items as items_api
from ...fabric.client import AccessDenied, FabricClient, FabricError
from .. import repository
from ..models import CodeArtifact, ItemSchedule, JobActivityRun, JobRun

log = logging.getLogger("lineage.collect.jobs")

# Item types with a Fabric job history.
JOB_ITEM_TYPES = {"Notebook", "DataPipeline", "Dataflow", "SparkJobDefinition", "CopyJob"}
# ...and the one whose runs are Power BI refreshes.
MODEL_TYPE = "SemanticModel"

FINISHED = {"completed", "failed", "cancelled", "deduped", "disabled"}
# Activity runs are read for runs this recent; older ones are history only.
ACTIVITY_WINDOW_DAYS = 60
# Bound on activity-run queries per scan (each is one POST): the first scan of
# a busy tenant catches up over a few scans instead of taking an hour.
MAX_ACTIVITY_QUERIES = 400


def _parse_ts(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        value = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.astimezone(dt.UTC).replace(tzinfo=None) if value.tzinfo else value


def _iso(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def collect(
    session: Session,
    client: FabricClient,
    *,
    customer_id: str,
    tenant_id: str,
    contributor_workspaces: set[str],
    only_items: set[str] | None = None,
    with_schedules: bool = True,
) -> dict:
    """
    Runs, schedules and activity runs of every runnable item in scope.

    `only_items` limits the read to those item ids (the live refresher reads
    the busy ones every minute); `with_schedules=False` skips the schedules.
    The result's `changed` lists the items with a new run or a run whose state
    moved since the last read.
    """
    contributor_workspaces = {w.lower() for w in contributor_workspaces}
    errors: list[dict] = []
    counts = {"items": 0, "runs": 0, "new": 0, "schedules": 0, "activityRuns": 0}
    changed: set[str] = set()

    workspaces = {
        w.id: w for w in repository.list_workspaces(session, customer_id, tenant_id)
    }
    all_items = repository.list_items(session, customer_id, tenant_id)
    items = [
        i
        for i in all_items
        if i.item_type in JOB_ITEM_TYPES | {MODEL_TYPE}
        and (only_items is None or i.id in only_items)
    ]
    by_guid = {i.item_guid.lower(): i for i in all_items}

    def failed(item, workspace, e, what: str) -> None:
        errors.append(
            {
                "scope": "item",
                "scope_id": item.id,
                "scope_name": f"{workspace.name} / {item.display_name}",
                "reason": "needs_contributor" if isinstance(e, AccessDenied) else "error",
                "message": f"{what}: {str(e)[:400]}",
            }
        )

    for item in items:
        workspace = workspaces.get(item.workspace_id)
        if (
            workspace is None
            or workspace.workspace_guid.lower() not in contributor_workspaces
        ):
            continue
        try:
            if item.item_type == MODEL_TYPE:
                instances = [
                    _from_refresh(r)
                    for r in items_api.list_model_refreshes(
                        client, workspace.workspace_guid, item.item_guid
                    )
                ]
            else:
                instances = items_api.list_job_instances(
                    client, workspace.workspace_guid, item.item_guid
                )
        except (AccessDenied, FabricError) as e:
            # A model nobody refreshes, or one the principal cannot read, is
            # not worth an error line each; a runnable item is.
            if item.item_type != MODEL_TYPE:
                failed(item, workspace, e, "Run history unavailable")
            continue

        counts["items"] += 1
        if _store_runs(session, customer_id, tenant_id, item, instances, counts):
            changed.add(item.id)
        if with_schedules:
            counts["schedules"] += _store_schedules(
                session, client, customer_id, tenant_id, item, workspace
            )
        session.commit()

    # Activity runs of pipeline runs, newest first, bounded per scan.
    queries = 0
    since = dt.datetime.utcnow() - dt.timedelta(days=ACTIVITY_WINDOW_DAYS)
    pipelines = {i.id: i for i in items if i.item_type == "DataPipeline"}
    definitions = _pipeline_targets(session, tenant_id, pipelines, by_guid)
    read = set(
        session.scalars(
            select(JobActivityRun.job_run_id).where(JobActivityRun.tenant_id == tenant_id)
        )
    )
    candidates = session.scalars(
        select(JobRun)
        .where(
            JobRun.tenant_id == tenant_id,
            JobRun.item_id.in_(list(pipelines)),
            JobRun.started_at >= since,
        )
        .order_by(JobRun.started_at.desc())
    ).all()
    for run in candidates:
        finished = (run.status or "").lower() in FINISHED
        if finished and run.id in read:
            continue
        if queries >= MAX_ACTIVITY_QUERIES:
            break
        pipeline = pipelines[run.item_id]
        workspace = workspaces.get(pipeline.workspace_id)
        if workspace is None or workspace.workspace_guid.lower() not in contributor_workspaces:
            continue
        queries += 1
        start = run.started_at or since
        end = (run.ended_at or dt.datetime.utcnow()) + dt.timedelta(days=1)
        try:
            activities = items_api.query_activity_runs(
                client,
                workspace.workspace_guid,
                run.run_guid,
                updated_after=_iso(start - dt.timedelta(days=1)),
                updated_before=_iso(end),
            )
        except (AccessDenied, FabricError) as e:
            failed(pipeline, workspace, e, "Activity runs unavailable")
            continue
        changed.add(pipeline.id)
        counts["activityRuns"] += store_activity_runs(
            session,
            customer_id,
            tenant_id,
            run,
            activities,
            by_guid=by_guid,
            targets=definitions.get(pipeline.id, {}),
        )
        session.commit()

    link_child_runs(session, tenant_id)
    session.commit()
    return {"stats": counts, "errors": errors, "changed": sorted(changed)}


def _from_refresh(refresh: dict) -> dict:
    """A Power BI refresh in a job instance's shape."""
    status = {
        "Completed": "Completed",
        "Failed": "Failed",
        "Unknown": "InProgress",
        "Cancelled": "Cancelled",
        "Disabled": "Disabled",
    }.get(refresh.get("status") or "", refresh.get("status") or "")
    failure = ""
    raw = refresh.get("serviceExceptionJson")
    if raw:
        try:
            failure = json.loads(raw).get("errorDescription") or json.loads(raw).get(
                "errorCode", ""
            )
        except (ValueError, AttributeError):
            failure = str(raw)[:300]
    return {
        "id": refresh.get("requestId") or str(refresh.get("id") or ""),
        "jobType": "Refresh",
        "invokeType": refresh.get("refreshType") or "",
        "status": status,
        "startTimeUtc": refresh.get("startTime"),
        "endTimeUtc": refresh.get("endTime"),
        "failureReason": {"message": failure} if failure else None,
    }


def _store_runs(session, customer_id, tenant_id, item, instances, counts) -> bool:
    """Upsert one item's runs; True when one is new or its state or end moved."""
    moved = False
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
            moved = True
        ended = _parse_ts(inst.get("endTimeUtc"))
        if row.status != (inst.get("status") or "") or row.ended_at != ended:
            moved = True
        row.job_type = inst.get("jobType") or ""
        row.status = inst.get("status") or ""
        row.started_at = _parse_ts(inst.get("startTimeUtc"))
        row.ended_at = _parse_ts(inst.get("endTimeUtc"))
        kept = {
            k: v
            for k, v in (row.parameters or {}).items()
            if k not in ("invokeType", "rootActivityId", "failureReason")
        }
        row.parameters = {
            **kept,
            **{
                k: inst[k]
                for k in ("invokeType", "rootActivityId", "failureReason")
                if inst.get(k)
            },
        }
        counts["runs"] += 1
    return moved


def _store_schedules(session, client, customer_id, tenant_id, item, workspace) -> int:
    """Replace this item's schedules. Unreadable schedules leave the old ones."""
    found: list[tuple[str, str, bool, dict]] = []
    try:
        if item.item_type == MODEL_TYPE:
            raw = items_api.get_model_refresh_schedule(
                client, workspace.workspace_guid, item.item_guid
            )
            if raw.get("days") or raw.get("times"):
                found.append(
                    (
                        "",
                        "Refresh",
                        bool(raw.get("enabled")),
                        {
                            "type": "Weekly",
                            "weekdays": raw.get("days") or [],
                            "times": raw.get("times") or [],
                            "localTimeZoneId": raw.get("localTimeZoneId") or "UTC",
                        },
                    )
                )
        else:
            job_type = items_api.SCHEDULE_JOB_TYPES.get(item.item_type)
            if not job_type:
                return 0
            for schedule in items_api.list_schedules(
                client, workspace.workspace_guid, item.item_guid, job_type
            ):
                found.append(
                    (
                        str(schedule.get("id") or ""),
                        job_type,
                        bool(schedule.get("enabled")),
                        schedule.get("configuration") or {},
                    )
                )
    except (AccessDenied, FabricError) as e:
        log.debug("schedules of %s unavailable: %s", item.display_name, e)
        return 0
    session.execute(delete(ItemSchedule).where(ItemSchedule.item_id == item.id))
    for guid, job_type, enabled, config in found:
        session.add(
            ItemSchedule(
                customer_id=customer_id,
                tenant_id=tenant_id,
                item_id=item.id,
                schedule_guid=guid,
                job_type=job_type,
                enabled=enabled,
                config=config,
            )
        )
    return len(found)


# --- activity runs ------------------------------------------------------------
# Where an activity names the item it runs: its input, then its definition.
_TARGET_KEYS = (
    "notebookId",
    "pipelineId",
    "dataflowId",
    "datasetId",
    "semanticModelId",
    "sparkJobDefinitionId",
    "copyJobId",
    "itemId",
)


def _pipeline_targets(session, tenant_id, pipelines, by_guid) -> dict[str, dict[str, str]]:
    """pipeline item id -> {activity name: item id it runs}, from the definition."""
    out: dict[str, dict[str, str]] = {}
    if not pipelines:
        return out
    for artifact in session.scalars(
        select(CodeArtifact).where(
            CodeArtifact.tenant_id == tenant_id,
            CodeArtifact.item_id.in_(list(pipelines)),
        )
    ):
        try:
            definition = json.loads(artifact.text or "")
        except ValueError:
            continue
        targets = out.setdefault(artifact.item_id, {})
        for activity in walk_activities(definition):
            props = activity.get("typeProperties") or {}
            for key in _TARGET_KEYS:
                guid = str(props.get(key) or "").lower()
                if guid in by_guid:
                    targets[activity.get("name") or ""] = by_guid[guid].id
                    break
    return out


def walk_activities(definition: dict) -> list[dict]:
    """Every activity in a pipeline definition, inside ForEach / If / Switch / Until too."""
    found: list[dict] = []

    def walk(activities) -> None:
        for activity in activities or []:
            if not isinstance(activity, dict):
                continue
            found.append(activity)
            props = activity.get("typeProperties") or {}
            walk(props.get("activities"))
            walk(props.get("ifTrueActivities"))
            walk(props.get("ifFalseActivities"))
            walk(props.get("defaultActivities"))
            for case in props.get("cases") or []:
                walk((case or {}).get("activities"))

    walk(((definition or {}).get("properties") or {}).get("activities"))
    return found


def store_activity_runs(
    session,
    customer_id: str,
    tenant_id: str,
    run: JobRun,
    activities: list[dict],
    *,
    by_guid: dict,
    targets: dict[str, str],
) -> int:
    """Replace one run's activity runs with what the query returned."""
    session.execute(delete(JobActivityRun).where(JobActivityRun.job_run_id == run.id))
    n = 0
    for activity in activities:
        name = str(activity.get("activityName") or "")
        inputs = activity.get("input") if isinstance(activity.get("input"), dict) else {}
        output = activity.get("output") if isinstance(activity.get("output"), dict) else {}
        target = None
        for key in _TARGET_KEYS:
            guid = str(inputs.get(key) or "").lower()
            if guid in by_guid:
                target = by_guid[guid].id
                break
        target = target or targets.get(name)
        child_run = str(
            output.get("pipelineRunId")
            or output.get("runId")
            or (output.get("result") or {}).get("runId")
            or ""
        ).lower()
        error = activity.get("error") or {}
        message = (
            error.get("message") if isinstance(error, dict) else str(error or "")
        ) or ""
        started = _parse_ts(activity.get("activityRunStart"))
        ended = _parse_ts(activity.get("activityRunEnd"))
        session.add(
            JobActivityRun(
                customer_id=customer_id,
                tenant_id=tenant_id,
                job_run_id=run.id,
                activity_run_id=str(activity.get("activityRunId") or ""),
                name=name[:200],
                activity_type=str(activity.get("activityType") or "")[:60],
                status=str(activity.get("status") or "")[:30],
                started_at=started,
                ended_at=ended,
                target_item_id=target,
                target_run_guid=child_run[:80],
                error=message[:2000],
            )
        )
        n += 1
    return n


def link_child_runs(session, tenant_id: str) -> int:
    """
    Mark each run an activity started with the item that started it.

    Exact when the activity reports the child's run id. Otherwise a child run of
    the activity's target item that starts within its window is taken — only
    when exactly one fits, so a busy notebook is never pinned to the wrong parent.
    """
    runs_by_item: dict[str, list[JobRun]] = {}
    by_guid: dict[str, JobRun] = {}
    for run in session.scalars(select(JobRun).where(JobRun.tenant_id == tenant_id)):
        runs_by_item.setdefault(run.item_id, []).append(run)
        by_guid[run.run_guid] = run
    parent_item = {
        r.id: r.item_id for runs in runs_by_item.values() for r in runs
    }
    linked = 0
    for activity in session.scalars(
        select(JobActivityRun).where(
            JobActivityRun.tenant_id == tenant_id,
            JobActivityRun.target_item_id.is_not(None),
        )
    ):
        child = by_guid.get(activity.target_run_guid) if activity.target_run_guid else None
        if child is None and activity.started_at:
            window_end = activity.ended_at or activity.started_at + dt.timedelta(hours=12)
            fits = [
                r
                for r in runs_by_item.get(activity.target_item_id or "", [])
                if r.started_at
                and activity.started_at - dt.timedelta(seconds=90)
                <= r.started_at
                <= window_end
            ]
            if len(fits) == 1:
                child = fits[0]
                activity.target_run_guid = child.run_guid
        if child is None:
            continue
        parent = parent_item.get(activity.job_run_id)
        if parent and child.item_id != parent:
            child.invoked_by_item_id = parent
            params = dict(child.parameters or {})
            params["parentRunId"] = activity.job_run_id
            child.parameters = params
            linked += 1
    return linked
