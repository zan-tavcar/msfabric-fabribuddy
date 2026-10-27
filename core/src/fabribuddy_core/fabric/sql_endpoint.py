"""
Read-only T-SQL against warehouses and lakehouse SQL analytics endpoints.

Warehouse definitions are not available through `getDefinition`, so the code
that writes warehouse tables is read where it lives:

  sys.sql_modules                  views, procedures, functions (definitions)
  queryinsights.exec_requests_history
                                   write statements actually executed in the
                                   last 30 days — CTAS, INSERT, MERGE, COPY INTO;
                                   this is how dbt- or tool-built tables show up

Only the two statements below are ever sent, enforced by `run`, over a
connection opened with ApplicationIntent=ReadOnly. Authentication is an Entra
access token for the SQL audience passed to the driver — the client secret
never appears in a connection string.
"""

import base64
import logging
import struct
import uuid

from .client import API_FABRIC, SCOPE_FABRIC, FabricClient, FabricError

log = logging.getLogger("lineage.sql_endpoint")

SCOPE_SQL = "https://database.windows.net/.default"
_SQL_COPT_SS_ACCESS_TOKEN = 1256

SYSTEM_SCHEMAS = ("sys", "queryinsights", "INFORMATION_SCHEMA", "guest")

Q_MODULES = (
    "SELECT s.name AS schema_name, o.name AS object_name, o.type_desc, m.definition "
    "FROM sys.sql_modules m "
    "JOIN sys.objects o ON o.object_id = m.object_id "
    "JOIN sys.schemas s ON s.schema_id = o.schema_id "
    "WHERE s.name NOT IN ('sys', 'queryinsights', 'INFORMATION_SCHEMA', 'guest')"
)

# Warehouse collation is case-sensitive by default, hence UPPER() in the filter.
Q_HISTORY = (
    "SELECT TOP 2000 command, start_time "
    "FROM queryinsights.exec_requests_history "
    "WHERE start_time > DATEADD(day, -30, GETUTCDATE()) "
    "AND status = 'Succeeded' "
    "AND UPPER(command) NOT LIKE '%QUERYINSIGHTS%' "
    "AND (UPPER(command) LIKE '%INSERT %' OR UPPER(command) LIKE '%MERGE %' "
    "OR UPPER(command) LIKE '%CREATE TABLE%' OR UPPER(command) LIKE '%SELECT%INTO %' "
    "OR UPPER(command) LIKE '%UPDATE %' OR UPPER(command) LIKE '%COPY INTO%') "
    "ORDER BY start_time DESC"
)

_ALLOWED = frozenset({Q_MODULES, Q_HISTORY})


class SqlUnavailable(Exception):
    """The endpoint could not be reached or queried; the message says why."""


def workspace_host(azure_tenant_id: str, workspace_guid: str) -> str:
    """
    The workspace's SQL host: `<b32(tenant)>-<b32(workspace)>.datawarehouse…`.

    The same host serves every warehouse and SQL endpoint in the workspace
    (the database is the item's name). Encoding: each GUID's little-endian
    bytes, base32, lowercase, unpadded — as the Warehouse API's
    `properties.connectionString` returns it.
    """

    def b32(guid: str) -> str:
        raw = uuid.UUID(guid).bytes_le
        return base64.b32encode(raw).decode().rstrip("=").lower()

    return (
        f"{b32(azure_tenant_id)}-{b32(workspace_guid)}.datawarehouse.fabric.microsoft.com"
    )


def warehouse_host(client: FabricClient, workspace_guid: str, warehouse_guid: str) -> str:
    payload = client.get_json(
        f"{API_FABRIC}/workspaces/{workspace_guid}/warehouses/{warehouse_guid}",
        SCOPE_FABRIC,
    )
    host = (payload.get("properties") or {}).get("connectionString") or ""
    if not host:
        raise FabricError("The warehouse reported no SQL connection string.")
    return host


class ReadOnlyConnection:
    def __init__(self, conn):
        self._conn = conn

    def run(self, statement: str) -> list[tuple]:
        if statement not in _ALLOWED:
            raise SqlUnavailable("Refusing a statement outside the read-only allowlist.")
        cursor = self._conn.cursor()
        try:
            cursor.execute(statement)
            return [tuple(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 — closing must never mask the real error
            log.debug("closing the SQL connection failed", exc_info=True)


def connect(client: FabricClient, host: str, database: str) -> ReadOnlyConnection:
    try:
        import mssql_python
    except ImportError:
        raise SqlUnavailable(
            "The SQL driver (mssql-python) is not installed on this server."
        ) from None
    token = client.tokens.token(SCOPE_SQL).encode("utf-16-le")
    packed = struct.pack(f"<I{len(token)}s", len(token), token)
    try:
        conn = mssql_python.connect(
            f"Server={host},1433;Database={database};Encrypt=yes;"
            "TrustServerCertificate=no;ApplicationIntent=ReadOnly",
            attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: packed},
            timeout=30,
        )
    except Exception as e:  # noqa: BLE001 — driver errors vary; the text is what matters
        raise SqlUnavailable(f"Could not connect to {database}: {str(e)[:300]}") from None
    return ReadOnlyConnection(conn)
