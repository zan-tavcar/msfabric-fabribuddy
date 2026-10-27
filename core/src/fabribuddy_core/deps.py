"""
Shared FastAPI dependencies.

`current_customer_id` is where the signed-in user's organisation comes from.
Every route goes through it, so tenant isolation follows the account.
"""

from fastapi import Depends, HTTPException, Path, Request
from sqlalchemy.orm import Session

from . import auth, config, repository
from .db import get_session
from .models import ROLE_ADMIN, ROLE_CONTRIBUTOR, ConnectedTenant, UserAccount

__all__ = [
    "current_customer_id",
    "current_user",
    "default_tenant",
    "get_tenant",
    "require_admin",
]


def current_user(request: Request, session: Session = Depends(get_session)) -> UserAccount:
    """The signed-in account, from the session cookie. 401 when there is none."""
    user = auth.user_for_token(session, request.cookies.get(config.SESSION_COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    auth.touch(session, user)
    return user


def require_admin(request: Request, session: Session = Depends(get_session)) -> None:
    """
    Guard for everything that changes what is collected: the connection, the
    scope, the schedule, scans, members. Contributors and viewers do not.
    Open with FL_AUTH_REQUIRED=0, where there are no accounts to tell apart.
    """
    if not config.AUTH_REQUIRED:
        return
    if current_user(request, session).role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Only an admin can do this.")


def require_contributor(request: Request, session: Session = Depends(get_session)) -> None:
    """
    Guard for writing documentation (table and column descriptions): an admin
    or a contributor. A viewer reads only. Open with FL_AUTH_REQUIRED=0.
    """
    if not config.AUTH_REQUIRED:
        return
    if current_user(request, session).role not in (ROLE_ADMIN, ROLE_CONTRIBUTOR):
        raise HTTPException(
            status_code=403, detail="Only a contributor or an admin can edit descriptions."
        )


def require_unlocked(request: Request, session: Session = Depends(get_session)) -> None:
    """
    Guard for what an ended trial or lapsed plan no longer opens: the lineage
    graph, table pages, search, exports and who-sees-what. The two overview
    pages and Settings stay open, so the organisation can see what it has and
    choose a plan. 402 with `locked: true`, which the UI turns into its lock.
    """
    from . import entitlements

    customer_id = current_customer_id(request, session)
    if entitlements.is_locked(session, customer_id):
        trial = entitlements.subscription_for(session, customer_id).plan_code == "trial"
        message = (
            "Your trial has ended and the data collected during it has been deleted. "
            "Choose a plan, then reconnect and scan to start again."
            if trial
            else "Your plan has ended. Choose a plan to unlock this page — "
            "everything collected is kept."
        )
        raise HTTPException(status_code=402, detail={"locked": True, "message": message})


def current_customer_id(request: Request, session: Session = Depends(get_session)) -> str:
    """
    The organisation every query is scoped to.

    With accounts on, it is the signed-in user's; with FL_AUTH_REQUIRED=0
    (local development, tests) it is the single DEFAULT_CUSTOMER_ID, exactly as
    before accounts existed. Every repository call takes it, so isolation holds
    either way.
    """
    if not config.AUTH_REQUIRED:
        return config.DEFAULT_CUSTOMER_ID
    return current_user(request, session).customer_id


def get_tenant(
    tenant_id: str = Path(..., description="Connected-tenant id"),
    session: Session = Depends(get_session),
    customer_id: str = Depends(current_customer_id),
) -> ConnectedTenant:
    tenant = repository.get_tenant(session, customer_id, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Connected tenant not found.")
    return tenant


def default_tenant(
    session: Session = Depends(get_session),
    customer_id: str = Depends(current_customer_id),
) -> ConnectedTenant:
    """
    Resolves the tenant for routes that do not name one. Unambiguous with a
    single connection; otherwise requires FL_DEFAULT_TENANT_ID rather than
    guessing which tenant the caller meant.
    """
    tenants = repository.list_tenants(session, customer_id)
    if not tenants:
        raise HTTPException(
            status_code=409,
            detail=(
                "No tenant connected. POST /api/tenants with tenantId, clientId "
                "and clientSecret, or set FL_DEMO_MODE=1 to explore the demo data."
            ),
        )
    if len(tenants) == 1:
        return tenants[0]
    if config.DEFAULT_TENANT_ID:
        for t in tenants:
            if config.DEFAULT_TENANT_ID in (t.id, t.azure_tenant_id):
                return t
        raise HTTPException(
            status_code=409,
            detail=f"FL_DEFAULT_TENANT_ID={config.DEFAULT_TENANT_ID} matches no tenant.",
        )
    raise HTTPException(
        status_code=409,
        detail=f"{len(tenants)} tenants connected. Set FL_DEFAULT_TENANT_ID.",
    )
