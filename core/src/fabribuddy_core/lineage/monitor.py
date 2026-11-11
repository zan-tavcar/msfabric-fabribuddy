"""
Job Monitor: what runs, when, how long, and what it ran.

Built from the jobs collector's rows — `job_run` (one per Fabric job instance
or model refresh), `job_activity_run` (a pipeline run's activities, with their
own timings), `item_schedule` — and the parsed `item_invocation` chain (a
pipeline's activities, `notebook.run`, `runMultiple`, `%run`).

Four payloads:

- `jobs()` — every runnable item: schedule, next run, latest run, the last
  runs as bars, success rate, average duration, who runs it and what it runs.
- `runs()` — every run across items, newest first (like Airflow's DAG runs).
- `job()` — one item: its runs with the children each one executed (measured
  when Fabric reports them, structural when it doesn't), the whole chain
  around it, and its code.

- `live()` — what is running now (with the step it is on and how long it
  usually takes), what just finished, and what the schedules start next. The
  app polls it; the live refresher (fabribuddy_intenant.live) rewrites it every
  minute, a scan once.

Child notebooks run by `runMultiple`, `notebook.run` or `%run` execute inside
the parent's Spark session: Fabric reports no run of their own, so they are
shown from the code with `timing: "in-session"` — never with invented times.
"""

import datetime as dt
import re
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .collectors.jobs import walk_activities
from .models import (
    CodeArtifact,
    ItemInvocation,
    ItemSchedule,
    JobActivityRun,
    JobRun,
    LineageEdge,
)

RUNNABLE_TYPES = {
    "DataPipeline",
    "Notebook",
    "Dataflow",
    "SemanticModel",
    "SparkJobDefinition",
    "CopyJob",
}
TYPE_LABEL = {
    "DataPipeline": "Pipeline",
    "Notebook": "Notebook",
    "Dataflow": "Dataflow Gen2",
    "SemanticModel": "Semantic model",
    "SparkJobDefinition": "Spark job definition",
    "CopyJob": "Copy job",
}
# Runs older than this are not shown (Fabric itself keeps a bounded history).
WINDOW_DAYS = 60
MAX_RUNS_PER_ITEM = 400
MAX_RUNS_LIST = 3000
BARS = 24

_STATUS = {
    "completed": "success",
    "succeeded": "success",
    "success": "success",
    "failed": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "inprogress": "running",
    "in progress": "running",
    "running": "running",
    "queued": "queued",
    "notstarted": "queued",
    "skipped": "skipped",
    "deduped": "skipped",
    "disabled": "skipped",
}


def status_of(raw: str | None) -> str:
    """Fabric's and Power BI's status words, as one vocabulary."""
    return _STATUS.get((raw or "").strip().lower(), "unknown")


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def _seconds(start: dt.datetime | None, end: dt.datetime | None) -> float | None:
    if not start or not end:
        return None
    return round(max((end - start).total_seconds(), 0.0), 1)


def _trigger(run: JobRun, items: dict) -> str:
    """Scheduled, Manual, or the item that ran it."""
    if run.invoked_by_item_id and run.invoked_by_item_id in items:
        return f"Run by {items[run.invoked_by_item_id].display_name}"
    invoke = str((run.parameters or {}).get("invokeType") or "")
    return {
        "Scheduled": "Scheduled",
        "Manual": "Manual",
        "OnDemand": "Manual",
        "ViaApi": "API",
        "ViaXmlaEndpoint": "XMLA",
        "ViaEnhancedApi": "API",
        "Pipeline": "Pipeline",
    }.get(invoke, invoke or "Unknown")


def _failure(run: JobRun) -> str:
    reason = (run.parameters or {}).get("failureReason")
    if isinstance(reason, dict):
        return str(reason.get("message") or reason.get("errorCode") or "")
    return str(reason or "")


# --- schedules ------------------------------------------------------------------
# The Job Scheduler names Windows time zones; the common ones, as IANA.
_WINDOWS_TZ = {
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "India Standard Time": "Asia/Kolkata",
    "Singapore Standard Time": "Asia/Singapore",
    "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney",
}
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(_WINDOWS_TZ.get(name or "UTC", name or "UTC"))
    except Exception:  # noqa: BLE001 — unknown zone: UTC, and the label says which
        return ZoneInfo("UTC")


def _times(config: dict) -> list[tuple[int, int]]:
    out = []
    for raw in config.get("times") or []:
        try:
            hour, minute = str(raw).split(":")[:2]
            out.append((int(hour), int(minute)))
        except ValueError:
            continue
    return sorted(out)


def schedule_text(config: dict) -> str:
    kind = str(config.get("type") or "")
    zone = config.get("localTimeZoneId") or "UTC"
    times = ", ".join(f"{h:02d}:{m:02d}" for h, m in _times(config))
    if kind == "Cron":
        minutes = int(config.get("interval") or 0)
        if minutes and minutes % 1440 == 0:
            every = f"{minutes // 1440} days" if minutes > 1440 else "day"
        elif minutes and minutes % 60 == 0:
            every = f"{minutes // 60} hours" if minutes > 60 else "hour"
        else:
            every = f"{minutes} min"
        return f"Every {every}"
    if kind == "Daily":
        return f"Daily at {times} ({zone})"
    if kind == "Weekly":
        days = config.get("weekdays") or []
        names = (
            "every day"
            if len(days) == 7
            else ", ".join(str(d)[:3] for d in days)
        )
        return f"{names} at {times} ({zone})"
    return kind or "Scheduled"


def upcoming(config: dict, now: dt.datetime, horizon_days: int = 35, cap: int = 400) -> list[str]:
    """The schedule's next occurrences (UTC ISO) from `now` for `horizon_days`."""
    kind = str(config.get("type") or "")
    zone = _zone(config.get("localTimeZoneId") or "UTC")
    now_utc = now.replace(tzinfo=dt.UTC)
    end_utc = now_utc + dt.timedelta(days=horizon_days)
    stop = config.get("endDateTime")
    if stop:
        try:
            stop_at = dt.datetime.fromisoformat(str(stop).replace("Z", "+00:00"))
            stop_at = stop_at if stop_at.tzinfo else stop_at.replace(tzinfo=zone)
            end_utc = min(end_utc, stop_at.astimezone(dt.UTC))
        except ValueError:
            pass
    out: list[dt.datetime] = []
    if kind == "Cron":
        minutes = int(config.get("interval") or 0)
        if minutes <= 0:
            return []
        try:
            start = dt.datetime.fromisoformat(
                str(config.get("startDateTime") or "").replace("Z", "+00:00")
            )
            start = start if start.tzinfo else start.replace(tzinfo=zone)
        except ValueError:
            start = now_utc
        step = dt.timedelta(minutes=minutes)
        current = start.astimezone(dt.UTC)
        if current < now_utc:
            current += step * ((now_utc - current) // step + 1)
        while current <= end_utc and len(out) < cap:
            out.append(current)
            current += step
    elif kind in ("Daily", "Weekly"):
        days = {str(d).lower() for d in config.get("weekdays") or []}
        local_day = now_utc.astimezone(zone).date()
        for offset in range(horizon_days + 1):
            day = local_day + dt.timedelta(days=offset)
            if kind == "Weekly" and _WEEKDAYS[day.weekday()].lower() not in days:
                continue
            for hour, minute in _times(config):
                at = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
                at_utc = at.astimezone(dt.UTC)
                if now_utc < at_utc <= end_utc:
                    out.append(at_utc)
            if len(out) >= cap:
                break
    return [o.replace(tzinfo=None).isoformat() + "Z" for o in sorted(out)[:cap]]


# --- the chain ------------------------------------------------------------------
_RUN_MULTIPLE = re.compile(r"\.runMultiple\s*\(")


def _via(caller, callee, artifacts: dict[str, str]) -> str:
    """How a caller runs a callee: a label only, the edge itself is parsed."""
    if caller.item_type == "DataPipeline":
        return "Pipeline activity"
    text = artifacts.get(caller.id, "")
    name = re.escape(callee.display_name)
    if re.search(rf"%run\s+[\"']?[^\s\"']*{name}\b", text):
        return "%run"
    if _RUN_MULTIPLE.search(text) and re.search(rf"[\"'][^\"']*{name}[\"']", text):
        # Named in the DAG (or a list) handed to runMultiple.
        return "runMultiple" if not re.search(
            rf"notebook\.run\s*\(\s*[\"'][^\"']*{name}[\"']", text
        ) else "notebook.run"
    if re.search(rf"notebook\.run\s*\(", text):
        return "notebook.run"
    return "Notebook call"


class Monitor:
    """Everything the monitor pages read, loaded once."""

    def __init__(self, context):
        self.context = context
        session, tenant_id = context.session, context.tenant_id
        self.now = dt.datetime.utcnow()
        self.since = self.now - dt.timedelta(days=WINDOW_DAYS)
        self.items = {
            i.id: i for i in context.items.values() if i.item_type in RUNNABLE_TYPES
        }
        self.all_items = context.items
        self.runs: dict[str, list[JobRun]] = {}
        self.run_by_id: dict[int, JobRun] = {}
        self.run_by_guid: dict[str, JobRun] = {}
        for run in session.scalars(
            select(JobRun)
            .where(JobRun.tenant_id == tenant_id)
            .order_by(JobRun.started_at.desc())
        ):
            self.run_by_id[run.id] = run
            self.run_by_guid[run.run_guid] = run
            if run.started_at is None or run.started_at >= self.since:
                self.runs.setdefault(run.item_id, []).append(run)
        self.activities: dict[int, list[JobActivityRun]] = {}
        for activity in session.scalars(
            select(JobActivityRun)
            .where(JobActivityRun.tenant_id == tenant_id)
            .order_by(JobActivityRun.started_at)
        ):
            self.activities.setdefault(activity.job_run_id, []).append(activity)
        self.schedules: dict[str, list[ItemSchedule]] = {}
        for schedule in session.scalars(
            select(ItemSchedule).where(ItemSchedule.tenant_id == tenant_id)
        ):
            self.schedules.setdefault(schedule.item_id, []).append(schedule)
        self.artifacts: dict[str, list[CodeArtifact]] = {}
        for artifact in session.scalars(
            select(CodeArtifact).where(CodeArtifact.tenant_id == tenant_id)
        ):
            if artifact.item_id in self.items:
                self.artifacts.setdefault(artifact.item_id, []).append(artifact)
        texts = {k: "\n".join(a.text or "" for a in v) for k, v in self.artifacts.items()}
        self.children: dict[str, list[tuple[str, str]]] = {}
        self.parents: dict[str, list[str]] = {}
        for from_id, to_id in session.execute(
            select(ItemInvocation.from_item_id, ItemInvocation.to_item_id).where(
                ItemInvocation.tenant_id == tenant_id
            )
        ):
            if from_id not in self.items or to_id not in self.items:
                continue
            via = _via(self.items[from_id], self.items[to_id], texts)
            self.children.setdefault(from_id, []).append((to_id, via))
            self.parents.setdefault(to_id, []).append(from_id)
        self.depends, self.containers = self._pipeline_dependencies()
        self.reads, self.writes = self._tables()

    def _tables(self) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """
        Per runnable item, the tables it reads and the tables it writes, from
        the lineage edges it carries (an edge A -> B via X: X reads A, writes B).
        """
        reads: dict[str, set[str]] = {}
        writes: dict[str, set[str]] = {}
        for from_id, to_id, via in self.context.session.execute(
            select(LineageEdge.from_node_id, LineageEdge.to_node_id, LineageEdge.via_item_id).where(
                LineageEdge.tenant_id == self.context.tenant_id
            )
        ):
            if via not in self.items:
                continue
            reads.setdefault(via, set()).add(from_id)
            writes.setdefault(via, set()).add(to_id)
        return reads, writes

    def table_name(self, node_id: str) -> str:
        node = self.context.nodes.get(node_id)
        if node is None:
            return node_id
        table = f"{node.schema_name}.{node.table_name}" if node.schema_name else node.table_name
        holder = self.all_items.get(node.item_id) if node.item_id else None
        return f"{holder.display_name} · {table}" if holder else (table or node.display_path)

    def data_dependencies(self) -> list[dict]:
        """
        Item A feeds item B when B reads a table A writes: a dependency on data,
        whether or not anything orders the two runs.
        """
        writers: dict[str, set[str]] = {}
        for item_id, tables in self.writes.items():
            for table in tables:
                writers.setdefault(table, set()).add(item_id)
        pairs: dict[tuple[str, str], set[str]] = {}
        for reader, tables in self.reads.items():
            for table in tables:
                for writer in writers.get(table, ()):
                    if writer != reader:
                        pairs.setdefault((writer, reader), set()).add(table)
        return [
            {"from": a, "to": b, "tables": sorted(self.table_name(t) for t in tables)}
            for (a, b), tables in sorted(pairs.items())
        ]

    # -- helpers ---------------------------------------------------------------
    def _pipeline_dependencies(self):
        """
        Per pipeline: {activity: [activities it waits for]} and {activity: the
        ForEach / If / Switch / Until it sits in}, from the definition.
        """
        import json

        out: dict[str, dict[str, list[str]]] = {}
        containers: dict[str, dict[str, str]] = {}
        for item_id, artifacts in self.artifacts.items():
            if self.items[item_id].item_type != "DataPipeline":
                continue
            for artifact in artifacts:
                try:
                    definition = json.loads(artifact.text or "")
                except ValueError:
                    continue
                deps = out.setdefault(item_id, {})
                inside = containers.setdefault(item_id, {})
                for activity in walk_activities(definition):
                    props = activity.get("typeProperties") or {}
                    nested = [
                        *(props.get("activities") or []),
                        *(props.get("ifTrueActivities") or []),
                        *(props.get("ifFalseActivities") or []),
                        *(props.get("defaultActivities") or []),
                        *[a for case in props.get("cases") or [] for a in (case or {}).get("activities") or []],
                    ]
                    for child in nested:
                        if isinstance(child, dict) and child.get("name"):
                            inside[child["name"]] = activity.get("name") or ""
                    deps[activity.get("name") or ""] = [
                        d.get("activity")
                        for d in activity.get("dependsOn") or []
                        if isinstance(d, dict) and d.get("activity")
                    ]
        return out, containers

    def _ref(self, item_id: str) -> dict:
        item = self.all_items.get(item_id)
        if item is None:
            return {"id": item_id, "name": item_id, "type": "", "typeLabel": ""}
        return {
            "id": item.id,
            "name": item.display_name,
            "type": item.item_type,
            "typeLabel": TYPE_LABEL.get(item.item_type, item.item_type),
        }

    def _workspace(self, item) -> str:
        workspace = self.context.workspaces.get(item.workspace_id)
        return workspace.name if workspace else ""

    def _run_row(self, run: JobRun) -> dict:
        parent = None
        parent_run_id = (run.parameters or {}).get("parentRunId")
        if parent_run_id and parent_run_id in self.run_by_id:
            p = self.run_by_id[parent_run_id]
            parent = {**self._ref(p.item_id), "runId": p.run_guid}
        elif run.invoked_by_item_id:
            parent = {**self._ref(run.invoked_by_item_id), "runId": None}
        return {
            "runId": run.run_guid,
            "status": status_of(run.status),
            "rawStatus": run.status,
            "trigger": _trigger(run, self.all_items),
            "startedAt": _iso(run.started_at),
            "endedAt": _iso(run.ended_at),
            "durationSec": _seconds(run.started_at, run.ended_at),
            "error": _failure(run),
            "parent": parent,
        }

    def _schedule(self, item_id: str) -> dict | None:
        schedules = self.schedules.get(item_id) or []
        if not schedules:
            return None
        enabled = [s for s in schedules if s.enabled]
        shown = enabled or schedules
        nexts = sorted(
            {at for s in enabled for at in upcoming(s.config or {}, self.now)}
        )
        return {
            "text": "; ".join(schedule_text(s.config or {}) for s in shown),
            "enabled": bool(enabled),
            "upcoming": nexts[:400],
        }

    def summary(self, item_id: str) -> dict:
        item = self.items[item_id]
        runs = self.runs.get(item_id, [])
        finished = [r for r in runs if status_of(r.status) in ("success", "failed", "cancelled")]
        succeeded = [r for r in finished if status_of(r.status) == "success"]
        durations = [
            d for d in (_seconds(r.started_at, r.ended_at) for r in succeeded) if d is not None
        ]
        latest = runs[0] if runs else None
        return {
            **self._ref(item_id),
            "workspace": self._workspace(item),
            "workspaceId": item.workspace_id,
            **_link(self.context, item),
            "schedule": self._schedule(item_id),
            "latest": self._run_row(latest) if latest else None,
            "bars": [
                {
                    "runId": r.run_guid,
                    "status": status_of(r.status),
                    "startedAt": _iso(r.started_at),
                    "durationSec": _seconds(r.started_at, r.ended_at),
                }
                for r in reversed(runs[:BARS])
            ],
            "stats": {
                "runs": len(runs),
                "finished": len(finished),
                "succeeded": len(succeeded),
                "failed": sum(1 for r in finished if status_of(r.status) == "failed"),
                "successRate": round(100 * len(succeeded) / len(finished), 1)
                if finished
                else None,
                "avgSec": round(sum(durations) / len(durations), 1) if durations else None,
                "maxSec": max(durations) if durations else None,
            },
            "parents": [
                {**self._ref(p)} for p in sorted(set(self.parents.get(item_id, [])))
            ],
            "children": [
                {**self._ref(c), "via": via} for c, via in self.children.get(item_id, [])
            ],
            "reads": sorted(self.table_name(t) for t in self.reads.get(item_id, ()))[:60],
            "writes": sorted(self.table_name(t) for t in self.writes.get(item_id, ()))[:60],
        }

    # -- children of one run -----------------------------------------------------
    def run_children(self, run: JobRun, depth: int = 0, prefix: str = "", seen=None) -> list[dict]:
        """
        What one run executed, as a flat list with `parentKey` for nesting.

        A pipeline's activity runs (measured), each child pipeline's own
        activities beneath it (when its run is known), and for a notebook the
        notebooks it calls in-session (from the code, not timed).
        """
        seen = set(seen or ()) | {run.id}
        out: list[dict] = []
        item = self.all_items.get(run.item_id)
        if item is None or depth > 4:
            return out
        if item.item_type == "DataPipeline":
            deps = self.depends.get(item.id, {})
            inside = self.containers.get(item.id, {})
            for activity in self.activities.get(run.id, []):
                container = inside.get(activity.name)
                key = f"{prefix}{container}/{activity.name}" if container else f"{prefix}{activity.name}"
                target = activity.target_item_id
                child = self.run_by_guid.get(activity.target_run_guid) if activity.target_run_guid else None
                out.append(
                    {
                        "key": key,
                        "parentKey": f"{prefix}{container}"
                        if container
                        else (prefix.rstrip("/") or None),
                        "depth": depth + (1 if container else 0),
                        "name": activity.name,
                        "activityType": activity.activity_type,
                        "item": self._ref(target) if target else None,
                        "runId": child.run_guid if child else None,
                        "status": status_of(activity.status),
                        "startedAt": _iso(activity.started_at),
                        "endedAt": _iso(activity.ended_at),
                        "durationSec": _seconds(activity.started_at, activity.ended_at),
                        "error": activity.error,
                        "dependsOn": [f"{prefix}{d}" for d in deps.get(activity.name, [])],
                        "timing": "measured",
                    }
                )
                if child is not None and child.id not in seen:
                    out.extend(self.run_children(child, depth + 1, f"{key}/", seen))
                elif target and self.all_items.get(target) is not None and (
                    self.all_items[target].item_type == "Notebook"
                ):
                    out.extend(
                        self._in_session(target, depth + 1, f"{key}/", activity.status, set())
                    )
        elif item.item_type == "Notebook":
            out.extend(self._in_session(item.id, depth, prefix, run.status, set()))
        return out

    def _in_session(self, notebook_id: str, depth: int, prefix: str, status, seen) -> list[dict]:
        """Notebooks a notebook runs inside its own session: from the code only."""
        out: list[dict] = []
        if depth > 5 or notebook_id in seen:
            return out
        seen = seen | {notebook_id}
        for child_id, via in self.children.get(notebook_id, []):
            child = self.all_items.get(child_id)
            if child is None:
                continue
            key = f"{prefix}{child.display_name}"
            out.append(
                {
                    "key": key,
                    "parentKey": prefix.rstrip("/") or None,
                    "depth": depth,
                    "name": child.display_name,
                    "activityType": via,
                    "item": self._ref(child_id),
                    "runId": None,
                    # The parent's outcome: a failed child fails its caller,
                    # but which child failed is not reported.
                    "status": "success" if status_of(status) == "success" else "unknown",
                    "startedAt": None,
                    "endedAt": None,
                    "durationSec": None,
                    "error": "",
                    "dependsOn": [],
                    "timing": "in-session",
                }
            )
            out.extend(self._in_session(child_id, depth + 1, f"{key}/", status, seen))
        return out

    def chain(self, item_id: str) -> dict:
        """The item, everything above it and everything below it, as a graph."""
        nodes: dict[str, dict] = {}
        edges: set[tuple[str, str, str]] = set()

        def down(current: str, depth: int) -> None:
            for child, via in self.children.get(current, []):
                edges.add((current, child, via))
                if child not in nodes and depth < 8:
                    nodes[child] = self._ref(child)
                    down(child, depth + 1)

        def up(current: str, depth: int) -> None:
            for parent in self.parents.get(current, []):
                via = next((v for c, v in self.children.get(parent, []) if c == current), "")
                edges.add((parent, current, via))
                if parent not in nodes and depth < 8:
                    nodes[parent] = self._ref(parent)
                    up(parent, depth + 1)

        nodes[item_id] = self._ref(item_id)
        down(item_id, 0)
        up(item_id, 0)
        return {
            "nodes": [
                {
                    **n,
                    "focus": n["id"] == item_id,
                    "latest": self._latest_status(n["id"]),
                }
                for n in nodes.values()
            ],
            "edges": [{"from": a, "to": b, "via": v} for a, b, v in sorted(edges)],
        }

    def avg_seconds(self, item_id: str) -> float | None:
        durations = [
            d
            for d in (
                _seconds(r.started_at, r.ended_at)
                for r in self.runs.get(item_id, [])
                if status_of(r.status) == "success"
            )
            if d is not None
        ]
        return round(sum(durations) / len(durations), 1) if durations else None

    def _latest_status(self, item_id: str) -> str | None:
        runs = self.runs.get(item_id)
        return status_of(runs[0].status) if runs else None


def _link(context, item) -> dict:
    from .views import fabric_link

    return fabric_link(context, item)


# --- the three payloads -----------------------------------------------------------
def _monitor(context) -> Monitor:
    """One Monitor per context: the export asks for every item's page in turn."""
    cached = getattr(context, "_activity_monitor", None)
    if cached is None:
        cached = Monitor(context)
        context._activity_monitor = cached
    return cached


def jobs(context) -> dict:
    monitor = _monitor(context)
    rows = [monitor.summary(i) for i in monitor.items]
    rows.sort(key=lambda r: (r["latest"] or {}).get("startedAt") or "", reverse=True)
    return {
        "generatedAt": _iso(monitor.now),
        "windowDays": WINDOW_DAYS,
        "jobs": rows,
        "data": monitor.data_dependencies(),
    }


def runs(context) -> dict:
    monitor = _monitor(context)
    out = []
    for item_id, item_runs in monitor.runs.items():
        if item_id not in monitor.items:
            continue
        item = monitor.items[item_id]
        for run in item_runs:
            out.append(
                {
                    **monitor._run_row(run),
                    "item": monitor._ref(item_id),
                    "workspace": monitor._workspace(item),
                    "workspaceId": item.workspace_id,
                }
            )
    out.sort(key=lambda r: r["startedAt"] or "", reverse=True)
    return {"generatedAt": _iso(monitor.now), "windowDays": WINDOW_DAYS, "runs": out[:MAX_RUNS_LIST]}


def job(context, item_id: str) -> dict | None:
    monitor = _monitor(context)
    if item_id not in monitor.items:
        return None
    runs_ = monitor.runs.get(item_id, [])[:MAX_RUNS_PER_ITEM]
    return {
        "generatedAt": _iso(monitor.now),
        "windowDays": WINDOW_DAYS,
        "job": monitor.summary(item_id),
        "chain": monitor.chain(item_id),
        "runs": [
            {**monitor._run_row(run), "children": monitor.run_children(run)} for run in runs_
        ],
        "code": [
            {"partPath": a.part_path, "language": a.language, "text": a.text}
            for a in monitor.artifacts.get(item_id, [])
        ],
    }


# How far ahead `live()` lists scheduled runs, and how far back "just finished".
UPCOMING_HOURS = 12
RECENT_HOURS = 3
ACTIVE = ("running", "queued")


def live(context, source: dict | None = None) -> dict:
    """Running now, just finished, up next: the small answer the app polls."""
    monitor = _monitor(context)
    now = monitor.now
    running, recent = [], []
    for item_id, item_runs in monitor.runs.items():
        item = monitor.items.get(item_id)
        if item is None:
            continue
        for run in item_runs:
            state = status_of(run.status)
            row = {
                **monitor._run_row(run),
                "item": monitor._ref(item_id),
                "workspace": monitor._workspace(item),
                "workspaceId": item.workspace_id,
            }
            if state in ACTIVE:
                children = monitor.run_children(run)
                running.append(
                    {
                        **row,
                        "expectedSec": monitor.avg_seconds(item_id),
                        "activeSteps": [
                            c["name"] for c in children if c["status"] in ACTIVE
                        ],
                        "doneSteps": sum(
                            1
                            for c in children
                            if c["timing"] == "measured"
                            and c["status"] not in ACTIVE
                            and c["depth"] == 0
                        ),
                    }
                )
            elif run.ended_at and run.ended_at >= now - dt.timedelta(hours=RECENT_HOURS):
                recent.append(row)
    running.sort(key=lambda r: r["startedAt"] or "")
    recent.sort(key=lambda r: r["endedAt"] or "", reverse=True)
    horizon = now + dt.timedelta(hours=UPCOMING_HOURS)
    next_up = []
    for item_id, item in monitor.items.items():
        schedule = monitor._schedule(item_id)
        for at in (schedule or {}).get("upcoming", []):
            when = dt.datetime.fromisoformat(at.rstrip("Z"))
            if when > horizon:
                break
            next_up.append(
                {
                    "at": at,
                    "item": monitor._ref(item_id),
                    "workspace": monitor._workspace(item),
                    "workspaceId": item.workspace_id,
                    "expectedSec": monitor.avg_seconds(item_id),
                }
            )
    next_up.sort(key=lambda r: r["at"])
    return {
        "generatedAt": _iso(now),
        # "scan": written once by the scan. "live": rewritten by the live
        # refresher every `everySeconds`.
        "source": source or {"kind": "scan", "everySeconds": None},
        "running": running,
        "recent": recent[:40],
        "upcoming": next_up[:60],
    }


def hot_items(context, *, before_min: int = 3, after_min: int = 2) -> set[str]:
    """
    The items worth reading right now: those running or queued, everything
    their chain will start, and those a schedule starts within minutes.
    """
    monitor = _monitor(context)
    now = monitor.now
    hot: set[str] = set()
    for item_id, item_runs in monitor.runs.items():
        if item_id in monitor.items and any(
            status_of(r.status) in ACTIVE for r in item_runs[:20]
        ):
            hot.add(item_id)
    for item_id, schedules in monitor.schedules.items():
        if item_id not in monitor.items:
            continue
        for schedule in schedules:
            if not schedule.enabled:
                continue
            soon = upcoming(
                schedule.config or {},
                now - dt.timedelta(minutes=before_min),
                horizon_days=1,
                cap=3,
            )
            if soon and dt.datetime.fromisoformat(soon[0].rstrip("Z")) <= now + dt.timedelta(
                minutes=after_min
            ):
                hot.add(item_id)
    # What a hot item runs is about to start (or just did).
    frontier = list(hot)
    while frontier:
        current = frontier.pop()
        for child, _via in monitor.children.get(current, []):
            if child not in hot:
                hot.add(child)
                frontier.append(child)
    return hot


def ancestors(context, item_ids: set[str]) -> set[str]:
    """The items whose pages show these items' runs: everything above them."""
    monitor = _monitor(context)
    out = set(item_ids)
    frontier = list(item_ids)
    while frontier:
        for parent in monitor.parents.get(frontier.pop(), []):
            if parent not in out:
                out.add(parent)
                frontier.append(parent)
    return out


def item_ids(context) -> list[str]:
    """The items that get a page: every runnable item."""
    return [i.id for i in context.items.values() if i.item_type in RUNNABLE_TYPES]
