"""
What an organisation's subscription lets it do, enforced at the point of use.

One subscription per organisation covers both products (plans.py). Plans differ
only in how many workspaces they scan, so nearly everything here is about that
cap, trials and lapsing:

  * **A plan scans at most its number of workspaces** (the trial 3, Team 10,
    Business 40, Enterprise all). The shared workspace selection is refused
    above the cap (`check_scope`); a scan trims to it as well (`cap_scope`), so
    a downgrade never scans more than it pays for.
  * **An ended subscription keeps every read and stops scans**: what was
    collected stays theirs, it simply stops being refreshed.
  * **Admins** are capped at 5 per organisation (auth.py); users are unlimited.

The operator's own organisation (`is_platform_admin`) is not a customer: it is
never capped and never expires.
"""

import datetime as dt
import logging
import math

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config, plans
from .lineage.models import Workspace
from .models import (
    SUB_ACTIVE,
    SUB_PAST_DUE,
    SUB_STATUSES,
    SUB_TRIALING,
    ConnectedTenant,
    Customer,
    Invitation,
    Subscription,
    UserAccount,
    utcnow,
)

log = logging.getLogger("fabribuddy.entitlements")


# --- rows ------------------------------------------------------------------------
def _start_trial(sub: Subscription) -> None:
    plan = plans.PLANS[plans.TRIAL]
    sub.plan_code = plan.code
    sub.subscription_status = SUB_TRIALING
    sub.trial_ends_at = utcnow() + dt.timedelta(days=plan.trial_days)
    sub.current_period_end = None


def _local_operator(customer_id: str) -> bool:
    """FL_AUTH_REQUIRED=0: one local operator and nobody to bill."""
    return not config.AUTH_REQUIRED and customer_id == config.DEFAULT_CUSTOMER_ID


def customer_for(session: Session, customer_id: str, name: str = "") -> Customer:
    """The organisation row, created on first use (with its subscription)."""
    customer = session.get(Customer, customer_id)
    if customer is None:
        customer = Customer(
            id=customer_id,
            name=name or ("Local" if _local_operator(customer_id) else customer_id),
        )
        session.add(customer)
        session.flush()
    _subscription_row(session, customer)
    session.commit()
    return customer


def _subscription_row(session: Session, customer: Customer) -> Subscription:
    sub = session.scalars(
        select(Subscription).where(
            Subscription.customer_id == customer.id, Subscription.product == plans.SUITE
        )
    ).first()
    if sub is None:
        sub = Subscription(customer_id=customer.id, product=plans.SUITE)
        if _local_operator(customer.id):
            sub.plan_code = plans.ENTERPRISE
            sub.subscription_status = SUB_ACTIVE
        else:
            _start_trial(sub)
        session.add(sub)
        session.flush()
    return sub


def subscription_for(session: Session, customer_id: str) -> Subscription:
    customer = customer_for(session, customer_id)
    return _subscription_row(session, customer)


def open_organisation(session: Session, customer_id: str, name: str) -> Customer:
    """
    A registration's organisation, on a fresh trial.

    The very first account adopts DEFAULT_CUSTOMER_ID (data collected before
    accounts existed); if that row was created unlimited while sign-in was off,
    it is on a trial now that someone owns it.
    """
    customer = session.get(Customer, customer_id)
    if customer is None:
        customer = Customer(id=customer_id)
        session.add(customer)
        session.flush()
    customer.name = name
    _start_trial(_subscription_row(session, customer))
    session.commit()
    return customer


def apply_plan(
    session: Session,
    customer: Customer,
    plan_code: str,
    *,
    status: str | None = None,
    billing_period: str | None = None,
    period_days: int | None = None,
    billing_ref: str | None = None,
    notes: str | None = None,
) -> Subscription:
    """
    The single mutation point for subscription state.

    The operator console, self-serve Subscribe and the Stripe webhook all call
    this — so the provider is a handler, not a second implementation.
    """
    plan_code = plans.normalise(plan_code)
    if plan_code not in plans.PLANS:
        raise ValueError(f"Unknown plan. Valid: {', '.join(plans.PLANS)}")
    if status is not None and status not in SUB_STATUSES:
        raise ValueError(f"Unknown status. Valid: {', '.join(SUB_STATUSES)}")
    if billing_period is not None and billing_period != "monthly":
        raise ValueError("Billing is monthly.")

    sub = _subscription_row(session, customer)
    if plan_code == plans.TRIAL:
        _start_trial(sub)
        if period_days:
            sub.trial_ends_at = utcnow() + dt.timedelta(days=period_days)
        if status:
            sub.subscription_status = status
    else:
        sub.plan_code = plan_code
        sub.subscription_status = status or SUB_ACTIVE
        # Provisional until a provider's webhook becomes the source of truth.
        sub.current_period_end = utcnow() + dt.timedelta(days=period_days or 30)
    if billing_ref is not None:
        customer.billing_ref = billing_ref
    if notes is not None:
        customer.notes = notes
    session.commit()
    log.info("customer %s: now %s (%s)", customer.id, plan_code, sub.subscription_status)
    return sub


# --- state ------------------------------------------------------------------------
def days_until(when: dt.datetime) -> int:
    """Whole days, rounded up: a trial started a minute ago has 14 left, not 13."""
    return math.ceil((when - utcnow()).total_seconds() / 86400)


def plan_for(customer: Customer, sub: Subscription) -> plans.Plan:
    if customer.is_platform_admin:
        return plans.PLANS[plans.ENTERPRISE]
    return plans.get(sub.plan_code)


def expires_at(sub: Subscription) -> dt.datetime | None:
    """One "expires" answer whether trial or paid, so callers never ask which."""
    if sub.subscription_status == SUB_TRIALING:
        return sub.trial_ends_at
    return sub.current_period_end


def is_active(customer: Customer, sub: Subscription) -> bool:
    """
    Whether this subscription still entitles them to scan.

    past_due keeps working: dunning is a conversation, not a cut-off. An
    expired trial or lapsed period keeps its data and its sign-in.
    """
    if customer.is_platform_admin:
        return True
    now = utcnow()
    if sub.subscription_status in (SUB_ACTIVE, SUB_PAST_DUE):
        return not (
            sub.subscription_status == SUB_ACTIVE
            and sub.current_period_end
            and sub.current_period_end < now
        )
    if sub.subscription_status == SUB_TRIALING:
        return sub.trial_ends_at is None or now <= sub.trial_ends_at
    return False


def subscription_active(session: Session, customer_id: str) -> bool:
    customer = customer_for(session, customer_id)
    return is_active(customer, _subscription_row(session, customer))


def _scoped_workspaces(session: Session, customer_id: str, real_tenants) -> int:
    """
    The workspaces the plan pays for: the shared selection when there is one,
    else every workspace a scan has stored (the "whole tenant" mode).
    """
    selected = session.scalars(
        select(ConnectedTenant).where(
            ConnectedTenant.customer_id == customer_id,
            ConnectedTenant.is_demo == 0,
            ConnectedTenant.scope_mode == "selected",
        )
    ).first()
    if selected is not None:
        return len(selected.selected_workspaces or [])
    return (
        session.scalar(
            select(func.count())
            .select_from(Workspace)
            .where(
                Workspace.customer_id == customer_id, Workspace.tenant_id.in_(real_tenants)
            )
        )
        or 0
    )


def usage(session: Session, customer_id: str) -> plans.Usage:
    def count(stmt) -> int:
        return session.scalar(stmt) or 0

    users = count(
        select(func.count())
        .select_from(UserAccount)
        .where(UserAccount.customer_id == customer_id, UserAccount.is_active == 1)
    )
    invitations = count(
        select(func.count())
        .select_from(Invitation)
        .where(
            Invitation.customer_id == customer_id,
            Invitation.accepted_at.is_(None),
            Invitation.expires_at > utcnow(),
        )
    )
    real_tenants = select(ConnectedTenant.id).where(
        ConnectedTenant.customer_id == customer_id, ConnectedTenant.is_demo == 0
    )
    tenants = count(select(func.count()).select_from(real_tenants.subquery()))
    return plans.Usage(
        users=users,
        invitations=invitations,
        workspaces=_scoped_workspaces(session, customer_id, real_tenants),
        tenants=tenants,
    )


def over_limits(plan: plans.Plan, u: plans.Usage) -> list[str]:
    return [
        name
        for name, used, cap in (
            ("users", u.seats, plan.max_users),
            ("workspaces", u.workspaces, plan.max_workspaces),
            ("tenants", u.tenants, plan.max_tenants),
        )
        if cap is not None and used > cap
    ]


def workspace_cap(session: Session, customer_id: str) -> int | None:
    """How many workspaces a scan may read; None = all of them."""
    customer = customer_for(session, customer_id)
    return plan_for(customer, _subscription_row(session, customer)).max_workspaces


def check_scope(session: Session, customer_id: str, mode: str, selected: list[str]) -> None:
    """The plan's workspace cap, when the Admin page saves the shared selection."""
    cap = workspace_cap(session, customer_id)
    if cap is None:
        return
    if mode != "selected" or len(selected) > cap:
        customer = customer_for(session, customer_id)
        plan = plan_for(customer, _subscription_row(session, customer))
        wanted = len(selected) if mode == "selected" else None
        upgrade = (
            plans.plan_for_workspaces(wanted) if wanted else plans.PLANS[plans.ENTERPRISE]
        )
        raise HTTPException(
            status_code=402,
            detail=(
                f"The {plan.name} plan scans up to {cap} workspaces. Select up to "
                f"{cap}, or move to {upgrade.name} "
                + (
                    f"({upgrade.max_workspaces} workspaces)."
                    if upgrade.max_workspaces
                    else "to scan the whole tenant."
                )
            ),
        )


def cap_scope(session: Session, customer_id: str, guids: list[str]) -> list[str]:
    """The scope a scan may use: unchanged, or the plan's first `cap`."""
    cap = workspace_cap(session, customer_id)
    return guids if cap is None else sorted(guids)[:cap]


def snapshot(session: Session, customer: Customer) -> dict:
    """Plan state for the UI: one subscription, both products."""
    from . import billing, paddle

    sub = _subscription_row(session, customer)
    session.commit()
    plan = plan_for(customer, sub)
    operator = bool(customer.is_platform_admin)
    # The operator has no subscription to end or to be nudged about.
    ends = None if operator else expires_at(sub)
    days_left = max(0, days_until(ends)) if ends else None
    status = SUB_ACTIVE if operator else sub.subscription_status
    u = usage(session, customer.id)
    return {
        "planCode": plan.code,
        "planName": plan.name,
        "priceMonth": plan.price_month,
        "currency": plans.CURRENCY,
        "status": status,
        "active": is_active(customer, sub),
        # Ended: only the overview pages and Settings open (deps.require_unlocked).
        "locked": not is_active(customer, sub),
        "trialDaysLeft": days_left if status == SUB_TRIALING else None,
        "expiresAt": ends.isoformat() if ends else None,
        "daysLeft": days_left,
        "billingPeriod": sub.billing_period,
        "limits": {
            "users": plan.max_users,
            "workspaces": plan.max_workspaces,
            "tenants": plan.max_tenants,
            "dailyScans": plan.max_daily_scans,
        },
        "products": [
            {"product": p, "name": plans.PRODUCT_NAMES[p]} for p in plans.PRODUCTS
        ],
        "organisation": customer.name,
        "usage": {
            "users": u.users,
            "invitations": u.invitations,
            "workspaces": u.workspaces,
            "tenants": u.tenants,
        },
        # The plan that fits the current selection, for the "you need" hint.
        "suggestedPlan": plans.plan_for_workspaces(u.workspaces).code,
        "over": over_limits(plan, u),
        "isPlatformAdmin": operator,
        # A provider configured: choosing a plan goes through its checkout, and
        # "Manage billing" opens its portal. paddle | stripe | manual.
        "billingProvider": config.billing_provider(),
        "billingEnabled": billing.enabled() or paddle.enabled(),
        "hasBillingAccount": bool(customer.billing_ref),
    }


# --- the end of a trial --------------------------------------------------------------
# A scan still marked running after this long crashed; it must not block deletion.
_STALE_RUN = dt.timedelta(hours=6)


def purge_ended_trials(session: Session) -> list[str]:
    """
    When a trial ends without a plan, delete what was collected from the tenant.

    The same as Settings → Disconnect: the stored client secret and every row
    collected for the tenant, both products. The account and organisation stay,
    so choosing a plan, reconnecting and scanning starts again from scratch.
    Paid periods that lapse are not purged here. Returns the purged tenant ids.
    """
    from . import repository as platform_repository
    from . import secrets
    from .models import KIND_SCANNER

    now = utcnow()
    purged: list[str] = []
    ended = session.scalars(
        select(Subscription).where(
            Subscription.subscription_status == SUB_TRIALING,
            Subscription.trial_ends_at.is_not(None),
            Subscription.trial_ends_at < now,
        )
    ).all()
    for sub in ended:
        customer = session.get(Customer, sub.customer_id)
        if customer is None or customer.is_platform_admin or _local_operator(customer.id):
            continue
        tenants = session.scalars(
            select(ConnectedTenant).where(
                ConnectedTenant.customer_id == customer.id, ConnectedTenant.is_demo == 0
            )
        ).all()
        for tenant in tenants:
            running = platform_repository.running_run(
                session, customer.id, tenant.id, KIND_SCANNER
            )
            if running is not None and running.started_at > now - _STALE_RUN:
                continue  # next tick, once the scan has stopped writing
            try:
                secrets.default_store().delete(tenant.secret_ref.split(":", 1)[-1])
            except (secrets.SecretUnavailable, AttributeError):
                pass
            platform_repository.delete_tenant(session, tenant)
            purged.append(tenant.id)
            log.warning(
                "trial ended for %s: tenant %s and its data deleted", customer.id, tenant.id
            )
    return purged


# --- enforcement ------------------------------------------------------------------
def is_locked(session: Session, customer_id: str) -> bool:
    """
    The trial (or paid period) has ended: only the overview pages and Settings
    open until a plan is chosen. The operator's organisation never locks.
    """
    return not subscription_active(session, customer_id)


def check_can_scan(session: Session, customer_id: str) -> None:
    if not subscription_active(session, customer_id):
        raise HTTPException(
            status_code=402,
            detail="Your FabriBuddy subscription has ended. Choose a plan to resume "
            "scanning.",
        )


def active_products(session: Session, customer_id: str) -> list[str]:
    """The products a pass runs: both while the subscription is active, else none."""
    return list(plans.PRODUCTS) if subscription_active(session, customer_id) else []


def check_can_add_user(session: Session, customer_id: str, *, pending: bool = True) -> None:
    """Users are unlimited on every plan; kept so a seat limit is one line away."""
    cap = plan_for(
        customer_for(session, customer_id), subscription_for(session, customer_id)
    ).max_users
    if cap is None:
        return
    u = usage(session, customer_id)
    taken = u.seats if pending else u.users
    if taken >= cap:
        raise HTTPException(
            status_code=402,
            detail=f"Your plan includes {cap} users and {taken} are taken. Upgrade to add more.",
        )


def check_can_add_tenant(session: Session, customer_id: str) -> None:
    """One connected tenant per organisation; both products use it."""
    u = usage(session, customer_id)
    if u.tenants >= 1:
        raise HTTPException(
            status_code=402,
            detail=(
                f"An organisation connects 1 tenant and you have {u.tenants}. "
                "Both products use it."
            ),
        )


def check_daily_scans(session: Session, customer_id: str, count: int) -> None:
    limit = plan_for(
        customer_for(session, customer_id), subscription_for(session, customer_id)
    ).max_daily_scans
    if count > limit:
        raise HTTPException(status_code=402, detail=f"Up to {limit} scheduled scans a day.")


def scheduled_times(
    session: Session, customer_id: str, times: list[str]
) -> list[str] | None:
    """
    The slots the scheduler may run for this organisation, or None to skip it
    (the subscription has ended).
    """
    if not subscription_active(session, customer_id):
        return None
    limit = plan_for(
        customer_for(session, customer_id), subscription_for(session, customer_id)
    ).max_daily_scans
    return sorted(times)[:limit]


def grant_platform_admin(session: Session, user: UserAccount) -> bool:
    """
    Flag the operator's organisation, if this is an FL_PLATFORM_ADMIN_EMAIL
    account. Called at startup and at registration, never from a request the
    customer controls the outcome of.
    """
    if user.email.lower() not in config.platform_admins():
        return False
    customer = customer_for(session, user.customer_id)
    if customer.is_platform_admin:
        return False
    customer.is_platform_admin = 1
    session.commit()
    log.warning("granted platform admin to %s", user.email)
    return True
