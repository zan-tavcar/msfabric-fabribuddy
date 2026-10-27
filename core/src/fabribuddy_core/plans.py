"""
The products and the plans.

FabriBuddy is two products in one app, sold together as **one subscription per
organisation**:

  * **FabriBuddy Data Catalog**: lineage and documentation for a Fabric estate
    (internal code `lineage`).
  * **FabriBuddy Report Audience**: who can see which Power BI report and who
    opens it (internal code `audience`).

Both products read the same workspace selection, so the plan is priced on the
one thing that drives cost and value alike, **the number of workspaces
scanned**:

  Trial       40 workspaces    7 days, free, no card (the Business allowance)
  Team        10 workspaces    €390 / month
  Business    40 workspaces    €690 / month
  Enterprise   unlimited       €1,690 / month

Prices are in euros, excluding VAT. The payment provider (Paddle, a merchant
of record) adds VAT or sales tax where it applies and issues the invoice.

Every plan has both products, every feature and unlimited users. A workspace
counts once however many products read it.

Plans live in code, not the database: the subscription row stores only its plan
code, so changing a price is a deploy. The payment provider stays the source of
truth for money; prices here are for display and MRR.
"""

from dataclasses import dataclass, field

# The two products: modules of the app, both included in every plan.
LINEAGE = "lineage"
AUDIENCE = "audience"
PRODUCTS = (LINEAGE, AUDIENCE)
PRODUCT_NAMES = {
    LINEAGE: "FabriBuddy Data Catalog",
    AUDIENCE: "FabriBuddy Report Audience",
}
PRODUCT_SHORT_NAMES = {LINEAGE: "Data Catalog", AUDIENCE: "Report Audience"}

# What a subscription row's `product` holds: one subscription covers both.
SUITE = "fabribuddy"
SUITE_NAME = "FabriBuddy"

# Every price is in this currency, excluding VAT. It must match the currency of
# the prices created in Paddle (FL_PADDLE_PRICE_ID_*).
CURRENCY = "EUR"

TRIAL = "trial"
TEAM = "team"
BUSINESS = "business"
ENTERPRISE = "enterprise"
PAID = (TEAM, BUSINESS, ENTERPRISE)


@dataclass(frozen=True)
class Plan:
    code: str
    name: str
    blurb: str
    # In CURRENCY, excluding VAT.
    price_month: int
    # None = unlimited.
    max_users: int | None
    max_workspaces: int | None
    max_tenants: int
    max_daily_scans: int
    trial_days: int = 0


PLANS: dict[str, Plan] = {
    TRIAL: Plan(
        code=TRIAL,
        name="Trial",
        blurb="Both products on up to 40 workspaces (the Business allowance) for 7 days, "
        "2 scheduled scans a day plus any you start. No card.",
        price_month=0,
        max_users=None,
        max_workspaces=40,
        max_tenants=1,
        # Scheduled only; "Scan now" is not counted. Keeps 20+ trials light.
        max_daily_scans=2,
        trial_days=7,
    ),
    TEAM: Plan(
        code=TEAM,
        name="Team",
        blurb="One data team's corner of the tenant: up to 10 workspaces.",
        price_month=390,
        max_users=None,
        max_workspaces=10,
        max_tenants=1,
        max_daily_scans=8,
    ),
    BUSINESS: Plan(
        code=BUSINESS,
        name="Business",
        blurb="Several teams and their reporting estate: up to 40 workspaces.",
        price_month=690,
        max_users=None,
        max_workspaces=40,
        max_tenants=1,
        max_daily_scans=8,
    ),
    ENTERPRISE: Plan(
        code=ENTERPRISE,
        name="Enterprise",
        blurb="The whole tenant, however many workspaces it has.",
        price_month=1690,
        max_users=None,
        max_workspaces=None,
        max_tenants=1,
        max_daily_scans=8,
    ),
}

DEFAULT_PLAN = TRIAL

# Codes from earlier pricing. Everything a customer paid for before had no
# workspace limit, so it maps to the plan that has none: nobody loses scope.
LEGACY = {"tenant": ENTERPRISE, "starter": ENTERPRISE, "growth": ENTERPRISE}

INCLUDED = {
    LINEAGE: [
        "Table lineage from source to report",
        "Which notebook, pipeline or dataflow writes each table",
        "Docs, catalog and CSV export",
        "Who can read each table, including indirectly",
        "Findings: stale tables, exposed PII, undocumented tables",
    ],
    AUDIENCE: [
        "Who can see each report: roles, direct shares, Entra groups",
        "Who actually opens it: the audit log, kept incrementally",
        "Pro / PPU licences nobody uses",
        "Dormant reports and models",
    ],
}
COMMON = [
    "Both products, every feature",
    "Unlimited users (up to 5 admins)",
    "Up to 8 scheduled scans a day",
]


def normalise(code: str | None) -> str:
    code = (code or "").lower()
    return LEGACY.get(code, code)


def get(code: str | None) -> Plan:
    """Unknown or missing codes fall back to the trial."""
    return PLANS.get(normalise(code), PLANS[DEFAULT_PLAN])


def plan_for_workspaces(count: int) -> Plan:
    """The smallest paid plan that covers `count` workspaces."""
    for code in PAID:
        cap = PLANS[code].max_workspaces
        if cap is None or count <= cap:
            return PLANS[code]
    return PLANS[ENTERPRISE]


def public_catalogue() -> dict:
    """What the plan page and the website show."""
    return {
        "products": [
            {"product": p, "name": PRODUCT_NAMES[p], "included": INCLUDED[p]}
            for p in PRODUCTS
        ],
        "common": COMMON,
        "currency": CURRENCY,
        "vatIncluded": False,
        "trial": {
            "days": PLANS[TRIAL].trial_days,
            "maxWorkspaces": PLANS[TRIAL].max_workspaces,
        },
        "plans": [
            {
                "code": plan.code,
                "name": plan.name,
                "blurb": plan.blurb,
                "priceMonth": plan.price_month,
                "maxWorkspaces": plan.max_workspaces,
            }
            for plan in (PLANS[c] for c in PAID)
        ],
    }


@dataclass
class Usage:
    """What an organisation is currently consuming."""

    users: int = 0
    invitations: int = 0  # pending; they hold a seat
    workspaces: int = 0
    tenants: int = 0
    over: list[str] = field(default_factory=list)

    @property
    def seats(self) -> int:
        return self.users + self.invitations
