"""
Reading the Delta transaction log over OneLake.

What the log gives us that nothing else does: who wrote a table and when
(`operation`, `operationParameters`, `engineInfo`), the write mode, the row and
byte counts from `add` actions' stats, and the version history behind
"381 Delta versions".

Only the newest commits are read. A table with 1,288 versions does not need its
whole history re-read on every scan — the collector asks for versions newer than
the highest it already holds, which is also why `delta_version` is append-only.

Checkpoints (Parquet) are read for one thing only: the table's schema, when
the JSON commits still present no longer carry it — Delta cleans up old
commits, so a table written for months has its `metaData` only in the latest
checkpoint. History is never rebuilt from them: a table whose early history
has been cleaned reports the versions still present, not an invented total.
"""

import datetime as dt
import json
import logging
import re

from .client import FabricClient, FabricError
from .onelake import list_paths, read_file

log = logging.getLogger("lineage.deltalog")

_COMMIT = re.compile(r"^(?P<version>\d{20})\.json$")

# Operations that mean "data was written", as Delta names them.
WRITE_OPERATIONS = {
    "WRITE",
    "MERGE",
    "DELETE",
    "UPDATE",
    "CREATE TABLE AS SELECT",
    "CREATE OR REPLACE TABLE AS SELECT",
    "REPLACE TABLE AS SELECT",
    "COPY INTO",
    "STREAMING UPDATE",
}
MAINTENANCE_OPERATIONS = {"OPTIMIZE", "VACUUM START", "VACUUM END"}

# How many commits to read per table per pass. A busy MERGE table gains
# thousands of versions between scans; reading them all would dominate the run,
# and the recent ones are what the UI shows.
MAX_COMMITS_PER_PASS = 40
# The fast path (read_recent): what a table needs is its last write and its
# row count, not its version history.
FIRST_PASS_COMMITS = 10  # newest-first, stopping at the latest full load
INCREMENTAL_COMMITS = 60  # everything new since the last pass, up to this

# Operations that replace the table's whole content: their row metric IS the
# table's row count.
FULL_LOAD_OPERATIONS = {
    "CREATE TABLE AS SELECT",
    "CREATE OR REPLACE TABLE AS SELECT",
    "REPLACE TABLE AS SELECT",
    "REPLACETABLE",  # Dataflow Gen2 (Mashup Engine) and Data Factory Copy
    "CREATE TABLE",
}
# Row metrics a commit may carry, kept so the row count can be tracked.
_ROW_METRICS = (
    "numOutputRows",
    "numTargetRowsInserted",
    "numTargetRowsDeleted",
    "numDeletedRows",
    "numCopiedRows",
    "numUpdatedRows",
)


class NoDeltaLog(FabricError):
    """The folder has no `_delta_log` — wrong path, or not a Delta table."""


def table_paths(lakehouse_guid: str, schema: str, table: str) -> list[str]:
    """
    Candidate OneLake paths to a table's folder, most likely first.

    Schema-enabled lakehouses nest under `Tables/<schema>/<table>`; classic ones
    put tables directly under `Tables/`. The table API reports `dbo` for both —
    synthetic on a classic lakehouse, a real folder on a schema-enabled one —
    so `dbo` cannot say which layout it is, and both are tried.
    """
    if schema and schema.lower() != "dbo":
        return [f"{lakehouse_guid}/Tables/{schema}/{table}"]
    flat = f"{lakehouse_guid}/Tables/{table}"
    return [flat, f"{lakehouse_guid}/Tables/{schema}/{table}"] if schema else [flat]


def table_path(lakehouse_guid: str, schema: str, table: str) -> str:
    return table_paths(lakehouse_guid, schema, table)[0]


def list_commit_versions(client: FabricClient, workspace_guid: str, path: str) -> list[int]:
    """Versions present in `_delta_log`, newest last."""
    entries = list_paths(client, workspace_guid, f"{path}/_delta_log")
    versions: list[int] = []
    for entry in entries:
        name = (entry.get("name") or "").rsplit("/", 1)[-1]
        match = _COMMIT.match(name)
        if match:
            versions.append(int(match.group("version")))
    return sorted(versions)


def commit_file_times(
    client: FabricClient, workspace_guid: str, path: str
) -> dict[int, dt.datetime]:
    """
    version -> the commit file's last-modified time in OneLake.

    The last resort for a commit that records no time at all (a Data Factory
    `Update` with no file actions): the file itself was written when the commit
    happened. One directory listing per table.
    """
    from email.utils import parsedate_to_datetime

    out: dict[int, dt.datetime] = {}
    for entry in list_paths(client, workspace_guid, f"{path}/_delta_log"):
        match = _COMMIT.match((entry.get("name") or "").rsplit("/", 1)[-1])
        modified = entry.get("lastModified")
        if not match or not modified:
            continue
        try:
            when = parsedate_to_datetime(modified)
        except (TypeError, ValueError):
            continue
        out[int(match.group("version"))] = when.astimezone(dt.UTC).replace(tzinfo=None)
    return out


def _parse_timestamp(value) -> dt.datetime | None:
    if value is None:
        return None
    try:
        # Delta records commit time as epoch milliseconds.
        return dt.datetime.fromtimestamp(int(value) / 1000, dt.UTC).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def read_commit(client: FabricClient, workspace_guid: str, path: str, version: int) -> dict:
    """
    One commit, summarised.

    Commit files are newline-delimited JSON. A malformed trailing line is normal
    while a writer is mid-flight, so those are skipped rather than failing.
    """
    name = f"{version:020d}.json"
    raw = read_file(client, workspace_guid, f"{path}/_delta_log/{name}")

    summary = {
        "version": version,
        "committed_at": None,
        "operation": "",
        "operation_parameters": {},
        "engine_info": "",
        "num_records": None,
        "size_bytes": 0,
        "num_files": 0,
        "schema": None,
        "table_properties": {},
        # Row bookkeeping for the fast path: the commit's metrics, the rows its
        # added files hold (from their stats), and whether it removed any file.
        "metrics": {},
        "added_records": 0,
        "added_any_stats": False,
        "removed_files": 0,
    }

    action_times: list = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            action = json.loads(line)
        except json.JSONDecodeError:
            continue

        if "commitInfo" in action:
            info = action["commitInfo"] or {}
            summary["committed_at"] = _parse_timestamp(
                info.get("timestamp") or info.get("inCommitTimestamp")
            )
            summary["operation"] = str(info.get("operation") or "")
            summary["operation_parameters"] = info.get("operationParameters") or {}
            summary["engine_info"] = str(info.get("engineInfo") or "")
            metrics = info.get("operationMetrics") or {}
            for key in _ROW_METRICS:
                if key in metrics:
                    try:
                        summary["metrics"][key] = int(metrics[key])
                    except (TypeError, ValueError):
                        pass
            for key in ("numOutputRows", "numTargetRowsInserted", "numCopiedRows"):
                if key in metrics:
                    try:
                        summary["num_records"] = int(metrics[key])
                        break
                    except (TypeError, ValueError):
                        pass

        elif "metaData" in action:
            metadata = action["metaData"] or {}
            raw_schema = metadata.get("schemaString")
            if raw_schema:
                try:
                    summary["schema"] = json.loads(raw_schema)
                except json.JSONDecodeError:
                    pass
            summary["table_properties"] = metadata.get("configuration") or {}

        elif "remove" in action:
            action_times.append((action["remove"] or {}).get("deletionTimestamp"))
            summary["removed_files"] += 1

        elif "add" in action:
            add = action["add"] or {}
            action_times.append(add.get("modificationTime"))
            summary["num_files"] += 1
            try:
                summary["size_bytes"] += int(add.get("size") or 0)
            except (TypeError, ValueError):
                pass
            stats = add.get("stats")
            if stats:
                try:
                    records = int((json.loads(stats) or {}).get("numRecords") or 0)
                except (json.JSONDecodeError, TypeError, ValueError):
                    records = None
                if records is not None:
                    summary["added_records"] += records
                    summary["added_any_stats"] = True
                    if summary["num_records"] is None:
                        summary["num_records"] = records

    if summary["committed_at"] is None:
        # Spark writes commitInfo.timestamp; Data Factory Copy and the Mashup
        # Engine (Dataflow Gen2) do not. The file actions still carry their own
        # times — the newest one is when this commit happened, near enough to
        # the second. Without it every pipeline- or dataflow-written table had
        # no last write, and the stale-table finding silently skipped it.
        times = []
        for value in action_times:
            try:
                times.append(int(value))
            except (TypeError, ValueError):
                continue
        if times:
            summary["committed_at"] = _parse_timestamp(max(times))

    return summary


def read_history(
    client: FabricClient,
    workspace_guid: str,
    path: str,
    *,
    since_version: int = -1,
    limit: int = MAX_COMMITS_PER_PASS,
) -> dict:
    """
    Commits newer than `since_version`, newest-first, plus the current shape.

    Reads at most `limit` commits, taking the NEWEST ones: a first scan of a
    table with 1,288 versions should show its recent history immediately rather
    than spending the whole run at version 0.
    """
    versions = list_commit_versions(client, workspace_guid, path)
    if not versions:
        raise NoDeltaLog(f"No Delta log at {path} — not a Delta table?")

    wanted = [v for v in versions if v > since_version]
    truncated = len(wanted) > limit
    wanted = wanted[-limit:]

    commits: list[dict] = []
    for version in reversed(wanted):
        try:
            commits.append(read_commit(client, workspace_guid, path, version))
        except FabricError as e:
            log.debug("commit %s unreadable at %s: %s", version, path, e)

    return {
        "commits": commits,
        "current_version": versions[-1],
        "total_versions": len(versions),
        "oldest_version_present": versions[0],
        "truncated": truncated,
    }


def read_recent(
    client: FabricClient, workspace_guid: str, path: str, *, since_version: int = -1
) -> dict:
    """
    The fast path: only the commits a table's last write and row count need.

    - Unchanged since the last pass: one listing, nothing read.
    - First pass: newest-first, stopping at the latest full load (whose row
      metric is the table's count) or after FIRST_PASS_COMMITS.
    - Later passes: every commit new since then, up to INCREMENTAL_COMMITS, so
      appends and merges can be added to the stored count; `contiguous` says
      whether that chain is complete.
    """
    versions = list_commit_versions(client, workspace_guid, path)
    if not versions:
        raise NoDeltaLog(f"No Delta log at {path} — not a Delta table?")
    current = versions[-1]
    base = {"current_version": current, "total_versions": len(versions)}
    if since_version >= current:
        return {**base, "commits": [], "unchanged": True, "contiguous": True}

    new = [v for v in versions if v > since_version]
    first = since_version < 0
    cap = FIRST_PASS_COMMITS if first else INCREMENTAL_COMMITS
    commits: list[dict] = []
    for version in reversed(new):
        try:
            commit = read_commit(client, workspace_guid, path, version)
        except FabricError as e:
            log.debug("commit %s unreadable at %s: %s", version, path, e)
            continue
        commits.append(commit)
        if len(commits) >= cap or (first and is_full_load(commit)):
            break
    return {
        **base,
        "commits": commits,
        "unchanged": False,
        "contiguous": not first and len(commits) == len(new),
        "truncated": not first and len(commits) < len(new),
    }


def is_full_load(commit: dict) -> bool:
    """A commit that replaced the table's whole content."""
    operation = (commit.get("operation") or "").upper()
    if operation in FULL_LOAD_OPERATIONS:
        return True
    mode = str((commit.get("operation_parameters") or {}).get("mode") or "").lower()
    return operation == "WRITE" and mode == "overwrite"


def _full_load_rows(commit: dict) -> int | None:
    metrics = commit.get("metrics") or {}
    if "numOutputRows" in metrics:
        return metrics["numOutputRows"]
    if commit.get("added_any_stats"):
        return commit.get("added_records") or 0
    return None


def _row_change(commit: dict) -> int | None:
    """How a non-full-load commit changed the row count; None when unknowable."""
    operation = (commit.get("operation") or "").upper()
    metrics = commit.get("metrics") or {}
    if (
        not operation
        or operation in MAINTENANCE_OPERATIONS
        or operation.startswith(
            (
                "VACUUM",
                "OPTIMIZE",
                "SET TBLPROPERTIES",
                "UPGRADE PROTOCOL",
                "CHANGE COLUMN",
                "ADD COLUMNS",
                "RENAME COLUMN",
            )
        )
    ):
        return 0
    if operation == "UPDATE":
        return 0
    if "MERGE" in operation:
        if "numTargetRowsInserted" in metrics or "numTargetRowsDeleted" in metrics:
            return metrics.get("numTargetRowsInserted", 0) - metrics.get(
                "numTargetRowsDeleted", 0
            )
        return None
    if operation == "DELETE":
        return -metrics["numDeletedRows"] if "numDeletedRows" in metrics else None
    if operation.startswith("TRUNCATE"):
        return None  # handled as a full load to zero by the caller
    if operation in ("WRITE", "STREAMING UPDATE", "COPY INTO") or (
        not commit.get("removed_files") and commit.get("added_any_stats")
    ):
        # An append: what it wrote is what it added.
        if "numOutputRows" in metrics:
            return metrics["numOutputRows"]
        if commit.get("added_any_stats"):
            return commit.get("added_records") or 0
    return None


def track_rows(
    commits_newest_first: list[dict],
    previous_rows: int | None,
    previous_exact: bool,
    contiguous: bool,
) -> tuple[int | None, bool]:
    """
    (row count, exact) after these commits.

    Exact from the latest full load forward; otherwise from the stored count
    when every commit since it was read; otherwise the stored count is kept and
    marked approximate — never a guess presented as a fact.
    """
    ordered = list(reversed(commits_newest_first))
    start, rows, exact = 0, None, False
    for index in range(len(ordered) - 1, -1, -1):
        commit = ordered[index]
        if (commit.get("operation") or "").upper().startswith("TRUNCATE"):
            start, rows, exact = index + 1, 0, True
            break
        if is_full_load(commit):
            full = _full_load_rows(commit)
            if full is not None:
                start, rows, exact = index + 1, full, True
                break
    if rows is None:
        if not (contiguous and previous_exact and previous_rows is not None):
            return previous_rows, False
        rows, exact = previous_rows, True
    for commit in ordered[start:]:
        change = _row_change(commit)
        if change is None:
            exact = False
        else:
            rows += change
    return max(rows, 0), exact


def read_checkpoint_metadata(client: FabricClient, workspace_guid: str, path: str) -> dict:
    """
    The table's current `metaData` from its latest checkpoint: schema and
    table properties. `{}` when there is no checkpoint or it cannot be read.

    Reads `_delta_log/_last_checkpoint` for the version (and part count), then
    the checkpoint file(s) until one holds the metaData row — it is in exactly
    one part of a multi-part checkpoint.
    """
    import io

    import pyarrow.parquet as pq

    try:
        pointer = json.loads(
            read_file(client, workspace_guid, f"{path}/_delta_log/_last_checkpoint")
        )
        version = int(pointer["version"])
        parts = int(pointer.get("parts") or 0)
    except (FabricError, ValueError, KeyError, TypeError) as e:
        log.debug("no readable _last_checkpoint at %s: %s", path, e)
        return {}

    names = (
        [
            f"{version:020d}.checkpoint.{i:010d}.{parts:010d}.parquet"
            for i in range(1, parts + 1)
        ]
        if parts
        else [f"{version:020d}.checkpoint.parquet"]
    )
    for name in names:
        try:
            raw = read_file(client, workspace_guid, f"{path}/_delta_log/{name}")
            rows = (
                pq.read_table(io.BytesIO(raw), columns=["metaData"]).column(0).to_pylist()
            )
        except Exception as e:  # noqa: BLE001 — a bad checkpoint is a gap, never a failed pass
            log.debug("checkpoint %s unreadable at %s: %s", name, path, e)
            continue
        metadata = next((r for r in rows if r and r.get("schemaString")), None)
        if metadata is None:
            continue
        out: dict = {"checkpoint_version": version}
        try:
            out["schema"] = json.loads(metadata["schemaString"])
        except json.JSONDecodeError:
            return {}
        configuration = metadata.get("configuration") or {}
        # Parquet maps come back as [(key, value), …].
        if isinstance(configuration, list):
            configuration = dict(configuration)
        out["table_properties"] = configuration
        return out
    return {}


# How far back a table's schema is looked for in JSON commits when neither the
# commits read this pass nor a checkpoint carry it.
SCHEMA_LOOKBACK = 50


def latest_metadata(client: FabricClient, workspace_guid: str, path: str) -> dict:
    """
    The table's current schema and properties, wherever they still are.

    The latest checkpoint holds them once the log has one; a young table has no
    checkpoint, and its schema is in an early commit (often version 0) that a
    first pass, stopping at the newest full load, never reads. So: checkpoint
    first, then the JSON commits newest-first. `{}` when neither has it.
    """
    found = read_checkpoint_metadata(client, workspace_guid, path)
    if found.get("schema"):
        return found
    for version in reversed(
        list_commit_versions(client, workspace_guid, path)[-SCHEMA_LOOKBACK:]
    ):
        try:
            commit = read_commit(client, workspace_guid, path, version)
        except FabricError:
            continue
        if commit.get("schema"):
            return {
                "schema": commit["schema"],
                "table_properties": commit.get("table_properties") or {},
            }
    return {}


def columns_from_schema(schema: dict | None) -> list[dict]:
    """
    Delta schema -> column dicts, including comments and nullability.

    A column's `metadata.comment` is where a description set with
    `ALTER TABLE … ALTER COLUMN … COMMENT` lives, which is how a customer can
    document a table in a way that travels with it rather than living in our DB.
    """
    if not isinstance(schema, dict):
        return []
    out: list[dict] = []
    for position, field in enumerate(schema.get("fields") or []):
        if not isinstance(field, dict) or not field.get("name"):
            continue
        field_type = field.get("type")
        if isinstance(field_type, dict):
            field_type = field_type.get("type") or "struct"
        metadata = field.get("metadata") or {}
        out.append(
            {
                "name": field["name"],
                "type": str(field_type or ""),
                "nullable": bool(field.get("nullable", True)),
                "comment": str(metadata.get("comment") or ""),
                "position": position,
            }
        )
    return out


def summarise(history: dict) -> dict:
    """Roll commits up into the stats the UI shows on a node's Overview tab."""
    commits = history.get("commits") or []
    writes = [c for c in commits if _is_write(c)]
    latest_write = writes[0] if writes else None

    last_optimize = next(
        (c["committed_at"] for c in commits if c["operation"].upper() == "OPTIMIZE"),
        None,
    )
    last_vacuum = next(
        (c["committed_at"] for c in commits if c["operation"].upper().startswith("VACUUM")),
        None,
    )

    return {
        "current_version": history.get("current_version"),
        "total_versions": history.get("total_versions"),
        "num_rows": latest_write.get("num_records") if latest_write else None,
        "size_bytes": sum(c.get("size_bytes") or 0 for c in commits) or None,
        "num_files": latest_write.get("num_files") if latest_write else None,
        "last_write_at": latest_write.get("committed_at") if latest_write else None,
        "last_operation": latest_write.get("operation") if latest_write else "",
        "last_engine": latest_write.get("engine_info") if latest_write else "",
        "last_optimize_at": last_optimize,
        "last_vacuum_at": last_vacuum,
        "schema": next((c["schema"] for c in commits if c.get("schema")), None),
        "table_properties": next(
            (c["table_properties"] for c in commits if c.get("table_properties")), {}
        ),
    }


def _is_write(commit: dict) -> bool:
    operation = (commit.get("operation") or "").upper()
    if operation in WRITE_OPERATIONS:
        return True
    # Delta's operation strings vary between engines and versions; anything that
    # is not recognised maintenance is treated as a write, because under-
    # reporting "last written" makes a live table look stale.
    return operation not in MAINTENANCE_OPERATIONS and bool(operation)


def write_mode_from(commit: dict) -> str:
    """The write mode a commit used, from its operation and parameters."""
    operation = (commit.get("operation") or "").upper()
    parameters = commit.get("operation_parameters") or {}
    if "MERGE" in operation:
        return "merge"
    if "REPLACE" in operation:
        return "overwrite"
    mode = str(parameters.get("mode") or "").lower()
    if mode in ("overwrite", "append"):
        return mode
    if operation.startswith("CREATE"):
        return "create"
    return mode or ""
