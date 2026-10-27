"""
The Fabric SQL database: what the app reads, and where the scanner keeps its state.

Three tables, created on first run:

- `dbo.ui_chunk` — every page's answer, packed (see `payloads.py`). The
  Fabric App's connector reads this table and nothing else.
- `dbo.fabribuddy_state` — the scanner's working database between runs
  (gzip of a SQLite file, in parts). Incremental scans, Delta history and the
  audit-log days already read depend on it.
- `dbo.fabribuddy_run` — one row per notebook run: when, how long, outcome.
- `dbo.fabribuddy_package` — the FabriBuddy package itself, put there by the
  deploy notebook, so the scan notebook installs it without uploads or GitHub.

Signs in with an Entra token for the SQL audience, never a password: the
service principal needs a workspace role (Contributor or above) on the
workspace that holds the database.
"""

import gzip
import logging
import struct
import time
from collections.abc import Iterable

log = logging.getLogger("fabribuddy.intenant.sqldb")

SCOPE_SQL = "https://database.windows.net/.default"
_SQL_COPT_SS_ACCESS_TOKEN = 1256
_STATE_PART = 8 * 2**20
# 3 parameters a row and SQL Server's 2,100-parameter limit.
_ROWS_PER_INSERT = 600

DDL = (
    """
    IF OBJECT_ID('dbo.ui_chunk', 'U') IS NULL
    CREATE TABLE dbo.ui_chunk (
        k NVARCHAR(64) NOT NULL,
        part INT NOT NULL,
        body NVARCHAR(4000) NOT NULL,
        CONSTRAINT pk_ui_chunk PRIMARY KEY (k, part)
    )
    """,
    """
    IF OBJECT_ID('dbo.fabribuddy_state', 'U') IS NULL
    CREATE TABLE dbo.fabribuddy_state (
        part INT NOT NULL CONSTRAINT pk_fabribuddy_state PRIMARY KEY,
        data VARBINARY(MAX) NOT NULL,
        saved_at DATETIME2 NOT NULL
    )
    """,
    """
    IF OBJECT_ID('dbo.fabribuddy_package', 'U') IS NULL
    CREATE TABLE dbo.fabribuddy_package (
        name NVARCHAR(200) NOT NULL CONSTRAINT pk_fabribuddy_package PRIMARY KEY,
        version NVARCHAR(50) NOT NULL,
        data VARBINARY(MAX) NOT NULL,
        saved_at DATETIME2 NOT NULL
    )
    """,
    """
    IF OBJECT_ID('dbo.fabribuddy_run', 'U') IS NULL
    CREATE TABLE dbo.fabribuddy_run (
        id INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_fabribuddy_run PRIMARY KEY,
        started_at DATETIME2 NOT NULL,
        finished_at DATETIME2 NULL,
        status NVARCHAR(20) NOT NULL,
        summary NVARCHAR(4000) NULL
    )
    """,
)


class SqlDatabaseError(Exception):
    """The Fabric SQL database could not be reached or written; the message says why."""


def connect(server: str, database: str, token: str):
    """A connection to the Fabric SQL database with an Entra access token."""
    raw = token.encode("utf-16-le")
    packed = struct.pack(f"<I{len(raw)}s", len(raw), raw)
    server = server.strip().removeprefix("tcp:").split(",")[0]
    conn_str = (
        f"Server={server},1433;Database={database};Encrypt=yes;TrustServerCertificate=no"
    )
    errors = []
    try:
        import mssql_python

        return mssql_python.connect(
            conn_str, attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: packed}, timeout=30
        )
    except ImportError:
        errors.append("mssql-python is not installed")
    except Exception as e:  # noqa: BLE001 — driver errors vary; the text is what matters
        errors.append(f"mssql-python: {str(e)[:300]}")
    # Fabric's runtimes also ship pyodbc with Microsoft's ODBC driver.
    try:
        import pyodbc

        return pyodbc.connect(
            f"Driver={{ODBC Driver 18 for SQL Server}};{conn_str}",
            attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: packed},
            timeout=30,
        )
    except ImportError:
        errors.append("pyodbc is not installed")
    except Exception as e:  # noqa: BLE001
        errors.append(f"pyodbc: {str(e)[:300]}")
    raise SqlDatabaseError(
        f"Could not connect to {database} on {server}. " + "; ".join(errors)
    )


def ensure_schema(conn) -> None:
    cursor = conn.cursor()
    try:
        for statement in DDL:
            cursor.execute(statement)
        conn.commit()
    finally:
        cursor.close()


# --- the scanner's state ----------------------------------------------------------
def load_state(conn) -> bytes | None:
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT data FROM dbo.fabribuddy_state ORDER BY part")
        parts = [bytes(row[0]) for row in cursor.fetchall()]
    finally:
        cursor.close()
    if not parts:
        return None
    return gzip.decompress(b"".join(parts))


def save_state(conn, data: bytes) -> int:
    packed = gzip.compress(data, compresslevel=6)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM dbo.fabribuddy_state")
        for i in range(0, len(packed), _STATE_PART):
            cursor.execute(
                "INSERT INTO dbo.fabribuddy_state (part, data, saved_at) "
                "VALUES (?, ?, SYSUTCDATETIME())",
                (i // _STATE_PART, packed[i : i + _STATE_PART]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
    return len(packed)


# --- what the app reads -----------------------------------------------------------
def replace_chunks(conn, rows: Iterable[tuple[str, int, str]]) -> int:
    """
    Swap in a whole new set of answers in one transaction.

    Readers see the previous set until the commit, never half of each.
    """
    rows = list(rows)
    cursor = conn.cursor()
    started = time.monotonic()
    try:
        cursor.execute("DELETE FROM dbo.ui_chunk")
        for i in range(0, len(rows), _ROWS_PER_INSERT):
            batch = rows[i : i + _ROWS_PER_INSERT]
            placeholders = ",".join(["(?, ?, ?)"] * len(batch))
            params = [value for row in batch for value in row]
            cursor.execute(
                f"INSERT INTO dbo.ui_chunk (k, part, body) VALUES {placeholders}", params
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
    log.info("wrote %d chunks in %.1fs", len(rows), time.monotonic() - started)
    return len(rows)


# --- the package --------------------------------------------------------------------
def save_package(conn, filename: str, version: str, data: bytes) -> None:
    """Keep exactly one wheel: the one the deploy notebook just installed."""
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM dbo.fabribuddy_package")
        cursor.execute(
            "INSERT INTO dbo.fabribuddy_package (name, version, data, saved_at) "
            "VALUES (?, ?, ?, SYSUTCDATETIME())",
            (filename, version, data),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


# --- run log ----------------------------------------------------------------------
def start_run(conn) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO dbo.fabribuddy_run (started_at, status) "
            "OUTPUT INSERTED.id VALUES (SYSUTCDATETIME(), 'running')"
        )
        run_id = int(cursor.fetchone()[0])
        conn.commit()
        return run_id
    finally:
        cursor.close()


def finish_run(conn, run_id: int, status: str, summary: str) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE dbo.fabribuddy_run SET finished_at = SYSUTCDATETIME(), status = ?, "
            "summary = ? WHERE id = ?",
            (status, summary[:4000], run_id),
        )
        conn.commit()
    finally:
        cursor.close()
