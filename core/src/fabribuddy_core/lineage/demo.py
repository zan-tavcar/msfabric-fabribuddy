"""
The demo scenario, seeded when FL_DEMO_MODE=1.

Two workspaces: **Sales Data [Prod]** (bronze/silver/gold lakehouses fed by a
pipeline, two PySpark notebooks, a `%%sql` notebook and a Dataflow Gen2) and
**Sales BI [Prod]** (a fixed-identity Direct Lake model and its report).

The important property: **the demo runs the real pipeline.** The notebook,
pipeline, mashup, TMDL and PBIR sources in `demo_data/` are stored as
`code_artifact` rows and then put through the actual parsers and resolver. The
edges and column edges you see in the UI were derived, not typed in — so a
parser regression breaks the demo visibly instead of hiding behind fixtures. The
same files back the golden tests in `tests/test_parsers.py`.

Only the parts a real tenant supplies over the network are fabricated: Delta
commit history, access principals and the OneLake roles.

Seeding is idempotent; every write is an upsert on a deterministic id.
"""

import datetime as dt
import hashlib
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from .. import repository as core_repository
from ..models import STATUS_CONNECTED, ConnectedTenant
from . import findings as findings_engine
from . import ids, layers, repository
from .collectors import access as access_collector
from .collectors import definitions as definitions_collector
from .models import (
    DOC_DELTA_PROPERTY,
    KIND_LAKEHOUSE_TABLE,
    KIND_MODEL_TABLE,
    CodeArtifact,
    DeltaStats,
    DeltaVersion,
)

log = logging.getLogger("lineage.demo")

DEMO_DATA = Path(__file__).parent / "demo_data"

# Fixed GUIDs: the ids derived from them are asserted by tests and appear in
# URLs, so they must not change between runs or releases.
WS_DATA = "11111111-1111-4111-8111-111111111111"
WS_BI = "22222222-2222-4222-8222-222222222222"

LH_BRONZE = "a1000000-0000-4000-8000-000000000001"
LH_SILVER = "a1000000-0000-4000-8000-000000000002"
LH_GOLD = "a1000000-0000-4000-8000-000000000003"

NB_SILVER_CUSTOMERS = "b1000000-0000-4000-8000-000000000001"
NB_SILVER_SALES = "b1000000-0000-4000-8000-000000000002"
NB_GOLD_MODEL = "b1000000-0000-4000-8000-000000000003"
PL_INGEST = "c1000000-0000-4000-8000-000000000001"
PL_DAILY = "c1000000-0000-4000-8000-000000000002"
DF_FX_RATES = "d1000000-0000-4000-8000-000000000001"
SM_SALES = "e1000000-0000-4000-8000-000000000001"
RPT_SALES = "f1000000-0000-4000-8000-000000000001"

WS_DATA_NAME = "Sales Data [Prod]"
WS_BI_NAME = "Sales BI [Prod]"

# (schema, table, [(column, type, description, nullable)])
BRONZE = [
    (
        "d365",
        "custtable",
        [
            ("ACCOUNTNUM", "string", "", False),
            ("NAME", "string", "", True),
            ("CUSTGROUP", "string", "", True),
            ("COUNTRYREGIONID", "string", "", True),
            ("EMAIL", "string", "", True),
            ("DATAAREAID", "string", "", False),
        ],
    ),
    (
        "d365",
        "salestable",
        [
            ("SALESID", "string", "", False),
            ("CUSTACCOUNT", "string", "", True),
            ("CURRENCYCODE", "string", "", True),
            ("CREATEDDATETIME", "timestamp", "", True),
            ("DATAAREAID", "string", "", False),
        ],
    ),
    (
        "d365",
        "salesline",
        [
            ("SALESID", "string", "", False),
            ("LINENUM", "decimal(32,16)", "", False),
            ("ITEMID", "string", "", True),
            ("SALESQTY", "decimal(32,6)", "", True),
            ("LINEAMOUNT", "decimal(32,6)", "", True),
            ("LINEDISC", "decimal(32,6)", "", True),
            ("DATAAREAID", "string", "", False),
        ],
    ),
]

SILVER = [
    (
        "sales",
        "customers",
        [
            (
                "customer_id",
                "string",
                "Customer account number, trimmed and upper-cased",
                False,
            ),
            ("customer_name", "string", "Customer display name", True),
            ("customer_group", "string", "Customer group code", True),
            ("country_code", "string", "ISO country / region code", True),
            ("email", "string", "Primary contact email, lower-cased", True),
            ("company", "string", "D365 legal entity", False),
            ("_loaded_at", "timestamp", "Load timestamp", True),
        ],
    ),
    (
        "sales",
        "sales_lines",
        [
            ("sales_id", "string", "Sales order id", False),
            ("line_num", "decimal(32,16)", "Line number within the order", False),
            ("customer_id", "string", "Ordering customer", True),
            ("order_date", "date", "Order date", True),
            ("item_id", "string", "Item number", True),
            ("quantity", "decimal(32,6)", "Ordered quantity", True),
            ("currency", "string", "Order currency", True),
            ("gross_amount", "decimal(32,6)", "Line amount before discount", True),
            ("discount_amount", "decimal(32,6)", "Line discount", True),
            (
                "net_amount",
                "decimal(32,6)",
                "Line amount after discount, order currency",
                True,
            ),
            ("company", "string", "D365 legal entity", False),
        ],
    ),
    (
        "ref",
        "fx_rates",
        [
            ("rate_date", "date", "Rate date", False),
            ("currency", "string", "ISO currency", False),
            ("rate_to_eur", "double", "Multiply an amount by this to get EUR", True),
        ],
    ),
]

GOLD = [
    (
        "dbo",
        "dim_customer",
        [
            ("customer_key", "bigint", "Surrogate key", False),
            ("customer_id", "string", "Business key", False),
            ("customer_name", "string", "Customer display name", True),
            ("customer_group", "string", "Customer group code", True),
            ("country_code", "string", "ISO country code", True),
        ],
    ),
    (
        "dbo",
        "fact_sales",
        [
            ("sales_id", "string", "Sales order id", False),
            ("line_num", "decimal(32,16)", "Line number", False),
            ("customer_key", "bigint", "FK to dim_customer", False),
            ("order_date", "date", "Order date", True),
            ("item_id", "string", "Item number", True),
            ("quantity", "decimal(32,6)", "Ordered quantity", True),
            ("net_amount", "decimal(32,6)", "Net amount, order currency", True),
            (
                "net_amount_eur",
                "decimal(38,6)",
                "Net amount converted to EUR at order date",
                True,
            ),
        ],
    ),
]

TABLE_DOCS = {
    ("sales", "customers"): (
        "Cleaned customer master, one row per customer and legal entity. Keys "
        "trimmed and upper-cased, names in title case.",
        "Data Platform team",
    ),
    ("sales", "sales_lines"): (
        "Order lines joined with their headers, one row per order line. Amounts "
        "in order currency.",
        "Data Platform team",
    ),
    ("ref", "fx_rates"): (
        "Daily FX rates to EUR, loaded from the Finance workbook.",
        "Finance",
    ),
    ("dbo", "dim_customer"): (
        "Customer dimension for the Sales star schema. Contains no PII — email "
        "is intentionally not carried over from silver.",
        "BI team",
    ),
    ("dbo", "fact_sales"): (
        "Sales fact at order-line grain, with amounts in order currency and EUR.",
        "BI team",
    ),
}

# Which artifact file backs each item, so the demo can store real code.
ARTIFACTS = {
    NB_SILVER_CUSTOMERS: [("notebook-content.py", "nb_silver_customers.py")],
    NB_SILVER_SALES: [("notebook-content.py", "nb_silver_sales.py")],
    NB_GOLD_MODEL: [("notebook-content.py", "nb_gold_model.py")],
    PL_INGEST: [("pipeline-content.json", "pl_ingest_d365.json")],
    DF_FX_RATES: [
        ("mashup.pq", "df_fx_rates.pq"),
        ("queryMetadata.json", "df_fx_rates.queryMetadata.json"),
    ],
    SM_SALES: [
        ("definition/tables/Sales.tmdl", "sales_model.Sales.tmdl"),
        ("definition/tables/Customer.tmdl", "sales_model.Customer.tmdl"),
        ("definition/roles/Country.tmdl", "sales_model.Country.role.tmdl"),
    ],
    RPT_SALES: [
        ("definition.pbir", "rpt_definition.pbir"),
        (
            "definition/pages/Overview/visuals/v-map-country/visual.json",
            "rpt_map_visual.json",
        ),
        (
            "definition/pages/Overview/visuals/v-table-customer/visual.json",
            "rpt_table_visual.json",
        ),
    ],
}

# (schema, table) -> (row count, bytes, total versions, minutes past 06:00)
DELTA_SHAPE = {
    ("d365", "custtable"): (48_210, 6_400_000, 381, 4),
    ("d365", "salestable"): (1_204_332, 92_000_000, 381, 5),
    ("d365", "salesline"): (5_812_090, 432_000_000, 381, 7),
    ("sales", "customers"): (47_998, 5_700_000, 377, 9),
    ("sales", "sales_lines"): (5_790_114, 315_000_000, 1_288, 14),
    ("ref", "fx_rates"): (3_650, 210_000, 214, -14_400),  # deliberately stale
    ("dbo", "dim_customer"): (47_998, 4_100_000, 366, 19),
    ("dbo", "fact_sales"): (5_790_114, 224_000_000, 412, 21),
}

WRITE_OPERATIONS = {
    ("d365", "custtable"): ("WRITE", {"mode": "Append"}),
    ("d365", "salestable"): ("WRITE", {"mode": "Append"}),
    ("d365", "salesline"): ("WRITE", {"mode": "Append"}),
    ("sales", "customers"): ("WRITE", {"mode": "Overwrite"}),
    ("sales", "sales_lines"): (
        "MERGE",
        {"predicate": "t.sales_id = s.sales_id AND t.line_num = s.line_num"},
    ),
    ("ref", "fx_rates"): ("WRITE", {"mode": "Overwrite"}),
    ("dbo", "dim_customer"): ("CREATE OR REPLACE TABLE AS SELECT", {}),
    ("dbo", "fact_sales"): ("CREATE OR REPLACE TABLE AS SELECT", {}),
}

ENGINE = "Apache-Spark/3.5.1 Delta-Lake/3.2.0"

# Principals. Groups carry their members so expansion is exercised too.
GROUP_MEMBERS = {
    "g-sales-analysts": [
        {"name": f"Analyst {n}", "upn": f"analyst{n}@contoso.com", "type": "User"}
        for n in range(1, 39)
    ],
    "g-bi-developers": [
        {"name": f"BI Dev {n}", "upn": f"bidev{n}@contoso.com", "type": "User"}
        for n in range(1, 7)
    ],
    "g-finance-controllers": [
        {"name": f"Controller {n}", "upn": f"fin{n}@contoso.com", "type": "User"}
        for n in range(1, 6)
    ],
    "g-sales-app": [
        {"name": f"Sales user {n}", "upn": f"sales{n}@contoso.com", "type": "User"}
        for n in range(1, 143)
    ],
}

WORKSPACE_USERS = {
    WS_DATA: [
        {
            "name": "Data Platform Admins",
            "graphId": "g-dp-admins",
            "type": "Group",
            "right": "Admin",
        },
        {
            "name": "spn-fabric-cicd",
            "upn": "spn-fabric-cicd",
            "type": "App",
            "right": "Contributor",
        },
        {
            "name": "Ana Novak",
            "upn": "ana.novak@contoso.com",
            "type": "User",
            "right": "Member",
        },
    ],
    WS_BI: [
        {
            "name": "Data Platform Admins",
            "graphId": "g-dp-admins",
            "type": "Group",
            "right": "Admin",
        },
        {
            "name": "Ana Novak",
            "upn": "ana.novak@contoso.com",
            "type": "User",
            "right": "Member",
        },
    ],
}

ITEM_USERS = {
    SM_SALES: [
        {
            "name": "BI Developers",
            "graphId": "g-bi-developers",
            "type": "Group",
            "right": "Build",
        },
        {
            "name": "Sales app audience",
            "graphId": "g-sales-app",
            "type": "Group",
            "right": "Read",
        },
    ],
    RPT_SALES: [
        {
            "name": "Sales app audience",
            "graphId": "g-sales-app",
            "type": "Group",
            "right": "Read",
        },
    ],
}

ONELAKE_ROLES = {
    LH_SILVER: [
        {
            "name": "SalesReaders",
            "scope": "sales/*",
            "members": [
                {"name": "Sales Analysts", "graphId": "g-sales-analysts", "type": "Group"}
            ],
        },
        {
            "name": "FinanceReaders",
            "scope": "ref/*",
            "members": [
                {
                    "name": "Finance Controllers",
                    "graphId": "g-finance-controllers",
                    "type": "Group",
                }
            ],
        },
    ],
    LH_GOLD: [
        {
            "name": "GoldReaders",
            "scope": "*",
            "members": [
                {"name": "BI Developers", "graphId": "g-bi-developers", "type": "Group"}
            ],
        }
    ],
}


def ensure_tenant(
    session: Session, customer_id: str | None = None, name: str | None = None
) -> ConnectedTenant:
    """
    A demo tenant that looks connected but holds no credentials.

    `secret_ref` is empty on purpose: nothing should ever call Microsoft for this
    tenant, and an empty ref fails loudly at credentials_for() if something tries.
    """
    customer_id = customer_id or config.DEFAULT_CUSTOMER_ID
    existing = core_repository.get_tenant_by_azure_id(session, customer_id, "demo-tenant")
    if existing is not None:
        return existing
    tenant = ConnectedTenant(
        customer_id=customer_id,
        display_name=name or config.DEMO_TENANT_NAME,
        azure_tenant_id="demo-tenant",
        client_id="demo",
        secret_ref="",
        status=STATUS_CONNECTED,
        capabilities={
            "powerbi_admin": {"available": True},
            "fabric_api": {"available": True},
            "onelake": {"available": True},
        },
        contributor_workspaces=[WS_DATA, WS_BI],
        is_demo=1,
    )
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return tenant


def seed(session: Session, tenant: ConnectedTenant) -> dict:
    """Write the whole scenario, deriving lineage with the real parsers."""
    customer_id, tenant_id = tenant.customer_id, tenant.id
    counts: dict[str, int] = {}

    _inventory(session, customer_id, tenant_id, counts)
    _artifacts(session, customer_id, tenant_id, counts)

    parsed = definitions_collector.parse(
        session, customer_id=customer_id, tenant_id=tenant_id, force=True
    )
    counts.update({f"parsed_{k}": v for k, v in parsed["stats"].items()})

    _delta_history(session, customer_id, tenant_id, counts)
    _access(session, customer_id, tenant_id, counts)

    found = findings_engine.run_all(session, customer_id, tenant_id)
    counts.update({f"finding_{k}": v for k, v in found.items()})

    session.commit()
    log.info("Demo scenario seeded: %s", counts)
    return counts


# --- inventory -------------------------------------------------------------
def _inventory(session, customer_id, tenant_id, counts) -> None:
    counts.setdefault("workspaces", 0)
    counts.setdefault("items", 0)
    counts.setdefault("nodes", 0)
    counts.setdefault("columns", 0)

    def workspace(guid, name):
        ws_id = ids.workspace_id(guid)
        repository.upsert_workspace(
            session,
            customer_id,
            tenant_id,
            id=ws_id,
            workspace_guid=guid,
            name=name,
            ws_type="Workspace",
            state="Active",
            has_contributor=True,
        )
        counts["workspaces"] += 1
        return ws_id

    def item(ws_guid, ws_id, guid, item_type, name, **kw):
        item_id = ids.item_id(ws_guid, guid)
        repository.upsert_item(
            session,
            customer_id,
            tenant_id,
            id=item_id,
            workspace_id=ws_id,
            item_guid=guid,
            item_type=item_type,
            display_name=name,
            **kw,
        )
        counts["items"] += 1
        return item_id

    def table(ws_guid, ws_id, ws_name, lh_guid, lh_name, schema, name, columns):
        node_id = ids.lakehouse_table_id(ws_guid, lh_guid, schema, name)
        layer, declared = layers.resolve(KIND_LAKEHOUSE_TABLE, lh_name, schema, name)
        repository.upsert_node(
            session,
            customer_id,
            tenant_id,
            id=node_id,
            kind=KIND_LAKEHOUSE_TABLE,
            layer=layer,
            layer_is_declared=declared,
            workspace_id=ws_id,
            item_id=ids.item_id(ws_guid, lh_guid),
            schema_name=schema,
            table_name=name,
            display_path=f"{ws_name} / {lh_name} / {schema}.{name}",
        )
        counts["nodes"] += 1
        # Flush before the doc row: it carries a FK to this node, and within one
        # transaction SQLite checks that at statement time. A real collector
        # never hits this because the node was committed by an earlier pass.
        session.flush()
        for ordinal, (column, dtype, description, nullable) in enumerate(columns):
            repository.upsert_column(
                session,
                customer_id,
                tenant_id,
                id=ids.column_id(node_id, column),
                node_id=node_id,
                name=column,
                ordinal=ordinal,
                data_type=dtype,
                nullable=nullable,
                description=description,
            )
            counts["columns"] += 1
        doc = TABLE_DOCS.get((schema, name))
        if doc:
            repository.upsert_doc(
                session,
                customer_id,
                tenant_id,
                node_id=node_id,
                column_id=None,
                source=DOC_DELTA_PROPERTY,
                description=doc[0],
                owner=doc[1],
                tags=[],
            )
        return node_id

    data_ws = workspace(WS_DATA, WS_DATA_NAME)
    for guid, name in (
        (LH_BRONZE, "lh_bronze"),
        (LH_SILVER, "lh_silver"),
        (LH_GOLD, "lh_gold"),
    ):
        item(WS_DATA, data_ws, guid, "Lakehouse", name)
    item(WS_DATA, data_ws, NB_SILVER_CUSTOMERS, "Notebook", "nb_silver_customers")
    item(WS_DATA, data_ws, NB_SILVER_SALES, "Notebook", "nb_silver_sales")
    item(WS_DATA, data_ws, NB_GOLD_MODEL, "Notebook", "nb_gold_model")
    item(WS_DATA, data_ws, PL_INGEST, "DataPipeline", "pl_ingest_d365")
    item(WS_DATA, data_ws, PL_DAILY, "DataPipeline", "pl_daily_sales")
    item(WS_DATA, data_ws, DF_FX_RATES, "Dataflow", "df_fx_rates")

    for lh_guid, lh_name, tables in (
        (LH_BRONZE, "lh_bronze", BRONZE),
        (LH_SILVER, "lh_silver", SILVER),
        (LH_GOLD, "lh_gold", GOLD),
    ):
        for schema, name, columns in tables:
            table(WS_DATA, data_ws, WS_DATA_NAME, lh_guid, lh_name, schema, name, columns)

    bi_ws = workspace(WS_BI, WS_BI_NAME)
    model_item = item(
        WS_BI,
        bi_ws,
        SM_SALES,
        "SemanticModel",
        "Sales Model",
        storage_mode="DirectLake",
        uses_fixed_identity=True,
        endorsement="Certified",
    )
    item(WS_BI, bi_ws, RPT_SALES, "Report", "Sales Overview")

    # One node for the model, its tables as rows — what the Scanner gives a
    # real tenant. The TMDL pass links it to its sources and fills each row's
    # origin; PBIR links the report to it.
    node_id = ids.semantic_model_id(WS_BI, SM_SALES)
    repository.upsert_node(
        session,
        customer_id,
        tenant_id,
        id=node_id,
        kind=KIND_MODEL_TABLE,
        layer=layers.layer_for_kind(KIND_MODEL_TABLE),
        layer_is_declared=True,
        workspace_id=bi_ws,
        item_id=model_item,
        table_name="Sales Model",
        display_path=f"{WS_BI_NAME} / Sales Model",
    )
    counts["nodes"] += 1
    for ordinal, table_name in enumerate(("Customer", "Sales")):
        repository.upsert_column(
            session,
            customer_id,
            tenant_id,
            id=ids.column_id(node_id, table_name),
            node_id=node_id,
            name=table_name,
            ordinal=ordinal,
            data_type="table",
        )
        counts["columns"] += 1

    # The report's own node is created when its PBIR is parsed, like any
    # tenant's: one node per report, reading the model tables its visuals use.
    session.commit()


# --- code artifacts --------------------------------------------------------
def _artifacts(session, customer_id, tenant_id, counts) -> None:
    """Store the demo_data files as code_artifact rows, as a fetch would."""
    counts["artifacts"] = 0
    for item_guid, parts in ARTIFACTS.items():
        workspace_guid = WS_BI if item_guid in (SM_SALES, RPT_SALES) else WS_DATA
        item_id = ids.item_id(workspace_guid, item_guid)
        for part_path, filename in parts:
            path = DEMO_DATA / filename
            if not path.exists():  # pragma: no cover - packaging guard
                log.warning("demo artifact missing: %s", filename)
                continue
            text = path.read_text("utf-8")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            existing = session.scalars(
                select(CodeArtifact).where(
                    CodeArtifact.item_id == item_id,
                    CodeArtifact.part_path == part_path,
                )
            ).first()
            if existing is None:
                existing = CodeArtifact(
                    customer_id=customer_id,
                    tenant_id=tenant_id,
                    item_id=item_id,
                    part_path=part_path,
                )
                session.add(existing)
            existing.language = _language(filename)
            existing.text = text
            existing.sha256 = digest
            existing.fetch_mode = "api"
            counts["artifacts"] += 1
    session.commit()


def _language(filename: str) -> str:
    if filename.endswith(".py"):
        return "python"
    if filename.endswith(".pq"):
        return "m"
    if filename.endswith(".tmdl"):
        return "tmdl"
    if filename.endswith(".json"):
        return "json"
    return "text"


# --- Delta history ---------------------------------------------------------
def _delta_history(session, customer_id, tenant_id, counts) -> None:
    """
    Plausible commit history.

    Three commits per table at its observed cadence, because the stale rule needs
    at least three timestamps to infer one — seeding fewer would make the rule
    untestable in the demo, which is the opposite of the point.
    """
    counts["deltaVersions"] = 0
    today = dt.datetime.now(dt.UTC).replace(
        hour=6, minute=0, second=0, microsecond=0, tzinfo=None
    )

    lakehouse_for_schema = {
        "d365": LH_BRONZE,
        "sales": LH_SILVER,
        "ref": LH_SILVER,
        "dbo": LH_GOLD,
    }

    for (schema, table), (rows, size, total, offset_minutes) in DELTA_SHAPE.items():
        lh_guid = lakehouse_for_schema[schema]
        node_id = ids.lakehouse_table_id(WS_DATA, lh_guid, schema, table)
        if repository.get_node(session, customer_id, tenant_id, node_id) is None:
            continue

        operation, parameters = WRITE_OPERATIONS[(schema, table)]
        last_write = today + dt.timedelta(minutes=offset_minutes)

        for step in range(3):
            version = total - 1 - step
            committed = last_write - dt.timedelta(days=step)
            existing = session.scalars(
                select(DeltaVersion).where(
                    DeltaVersion.node_id == node_id, DeltaVersion.version == version
                )
            ).first()
            if existing is not None:
                continue
            session.add(
                DeltaVersion(
                    customer_id=customer_id,
                    tenant_id=tenant_id,
                    node_id=node_id,
                    version=version,
                    committed_at=committed,
                    operation=operation,
                    operation_parameters=parameters,
                    engine_info=ENGINE,
                )
            )
            counts["deltaVersions"] += 1

        stats = session.get(DeltaStats, node_id)
        if stats is None:
            stats = DeltaStats(
                node_id=node_id, customer_id=customer_id, tenant_id=tenant_id
            )
            session.add(stats)
        stats.current_version = total - 1
        stats.num_rows = rows
        stats.size_bytes = size
        stats.num_files = max(1, rows // 200_000)
        stats.last_write_at = last_write
        stats.last_optimize_at = last_write - dt.timedelta(days=2)
    session.commit()


# --- access ----------------------------------------------------------------
def _access(session, customer_id, tenant_id, counts) -> None:
    workspace_users = {
        ids.workspace_id(guid): users for guid, users in WORKSPACE_USERS.items()
    }
    item_users = {ids.item_id(WS_BI, guid): users for guid, users in ITEM_USERS.items()}
    onelake_roles = {
        ids.item_id(WS_DATA, guid): roles for guid, roles in ONELAKE_ROLES.items()
    }
    result = access_collector.collect(
        session,
        None,
        customer_id=customer_id,
        tenant_id=tenant_id,
        workspace_users=workspace_users,
        item_users=item_users,
        onelake_roles=onelake_roles,
        group_members=GROUP_MEMBERS,
    )
    counts.update({f"access_{k}": v for k, v in result["stats"].items()})
