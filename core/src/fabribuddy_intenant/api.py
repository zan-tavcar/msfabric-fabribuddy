"""
The routes the Fabric App reads — and only those.

The SaaS app (`fabribuddy_core.main`) also serves accounts, billing and the
operator console; none of that exists in-tenant, so this builds a FastAPI app
from the three routers the pages use: the Data Catalog, Report Audience and
scan status. The exporter asks it in-process; nothing listens on a port.
"""

from fastapi import FastAPI


def build() -> FastAPI:
    from fabribuddy_core.routers import audience, lineage, scans

    app = FastAPI(title="FabriBuddy (in-tenant)")
    app.include_router(lineage.router)
    app.include_router(lineage.scoped)
    app.include_router(scans.router)
    app.include_router(audience.router)
    return app


# What the SaaS answers from its account routes, as the in-tenant shell needs it:
# read-only (inTenant), never locked, both products.
AUTH_CONFIG = {
    "authRequired": False,
    "registrationOpen": False,
    "emailVerification": False,
    "inTenant": True,
}

SUBSCRIPTION = {
    "planCode": "enterprise",
    "planName": "In-tenant edition",
    "priceMonth": 0,
    "currency": "EUR",
    "status": "active",
    "active": True,
    "locked": False,
    "trialDaysLeft": None,
    "expiresAt": None,
    "daysLeft": None,
    "billingPeriod": "monthly",
    "limits": {"users": None, "workspaces": None, "tenants": 1, "dailyScans": 8},
    "products": [
        {"product": "lineage", "name": "Data Catalog"},
        {"product": "audience", "name": "Report Audience"},
    ],
    "organisation": "",
    "usage": {"users": 0, "invitations": 0, "workspaces": 0, "tenants": 1},
    "suggestedPlan": "enterprise",
    "over": [],
    "isPlatformAdmin": False,
    "billingProvider": "manual",
    "billingEnabled": False,
    "hasBillingAccount": False,
}
