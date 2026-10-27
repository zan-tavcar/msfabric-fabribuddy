"""
Accounts: password hashing, sessions, one-time tokens, login throttling.

Deliberately dependency-free. Passwords are hashed with scrypt from the
standard library (memory-hard, salted, parameters stored with the hash so they
can be raised later without invalidating existing accounts). A session is a
random 256-bit token in an HttpOnly cookie; only its SHA-256 is stored, so a
database leak does not hand out live sessions.
"""

import base64
import datetime as dt
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import config
from .models import (
    LEGACY_ROLES,
    MAX_ADMINS,
    ROLE_ADMIN,
    ROLES,
    AuthToken,
    Invitation,
    UserAccount,
    UserSession,
    utcnow,
)

INVITATION_DAYS = 7

# scrypt cost: N=2^15, r=8, p=1 — ~32 MB and ~50 ms per hash on a modern core.
_N, _R, _P = 2**15, 8, 1
_MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD_LENGTH = 10


class AuthError(Exception):
    """A failure the user can act on; the message is safe to show."""


class Unverified(AuthError):
    """The password is right but the e-mail address is not confirmed yet."""


# --- passwords -------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def check_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if password.lower() == password or password.upper() == password:
        raise AuthError("Mix upper- and lower-case letters.")
    if not any(ch.isdigit() for ch in password):
        raise AuthError("Include at least one digit.")
    if all(ch.isalnum() for ch in password):
        raise AuthError("Include at least one special character, such as ! ? # - _.")


def normalise_email(email: str) -> str:
    email = (email or "").strip().lower()
    local, _, domain = email.partition("@")
    if not local or "." not in domain or len(email) > 320:
        raise AuthError("Enter a valid e-mail address.")
    return email


# --- sessions --------------------------------------------------------------
def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(session: Session, user: UserAccount, user_agent: str = "") -> str:
    """Returns the raw token for the cookie; only its hash is stored."""
    token = secrets.token_urlsafe(32)
    session.add(
        UserSession(
            id=_digest(token),
            user_id=user.id,
            expires_at=utcnow() + dt.timedelta(days=config.SESSION_DAYS),
            user_agent=(user_agent or "")[:300],
        )
    )
    user.last_login_at = user.last_seen_at = utcnow()
    session.commit()
    return token


def user_for_token(session: Session, token: str | None) -> UserAccount | None:
    if not token:
        return None
    row = session.get(UserSession, _digest(token))
    if row is None or row.expires_at < utcnow():
        return None
    user = session.get(UserAccount, row.user_id)
    if user is None or not user.is_active:
        return None
    return user


SEEN_EVERY = dt.timedelta(minutes=5)


def touch(session: Session, user: UserAccount) -> None:
    """Record activity, at most once per SEEN_EVERY so reads stay reads."""
    now = utcnow()
    if user.last_seen_at is None or now - user.last_seen_at > SEEN_EVERY:
        user.last_seen_at = now
        session.commit()


def revoke_session(session: Session, token: str | None) -> None:
    if token:
        session.execute(delete(UserSession).where(UserSession.id == _digest(token)))
        session.commit()


def revoke_all_sessions(
    session: Session, user_id: str, keep_token: str | None = None
) -> None:
    """After a password change every other browser is signed out."""
    stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if keep_token:
        stmt = stmt.where(UserSession.id != _digest(keep_token))
    session.execute(stmt)
    session.commit()


# --- one-time tokens -------------------------------------------------------
def issue_token(session: Session, user: UserAccount, purpose: str, hours: int = 24) -> str:
    token = secrets.token_urlsafe(32)
    session.add(
        AuthToken(
            id=_digest(token),
            user_id=user.id,
            purpose=purpose,
            expires_at=utcnow() + dt.timedelta(hours=hours),
        )
    )
    session.commit()
    return token


def redeem_token(session: Session, token: str, purpose: str) -> UserAccount:
    row = session.get(AuthToken, _digest(token or ""))
    if row is None or row.purpose != purpose or row.used_at or row.expires_at < utcnow():
        raise AuthError("This link is invalid or has expired.")
    row.used_at = utcnow()
    user = session.get(UserAccount, row.user_id)
    if user is None:
        raise AuthError("This link is invalid or has expired.")
    session.commit()
    return user


# --- registration ----------------------------------------------------------
def register(
    session: Session,
    email: str,
    password: str | None,
    display_name: str = "",
    organisation: str = "",
    *,
    microsoft: tuple[str, str] | None = None,
) -> UserAccount:
    """
    A new organisation and its first admin. With `microsoft` (object id, tenant
    id) the account signs in with Microsoft: no password, and the address
    counts as confirmed because Microsoft signed the person in.
    """
    if not config.ALLOW_REGISTRATION:
        raise AuthError("Registration is closed on this instance.")
    email = normalise_email(email)
    if microsoft is None:
        check_password_policy(password or "")
    if session.scalars(select(UserAccount).where(UserAccount.email == email)).first():
        raise AuthError("An account with this e-mail already exists.")

    # The very first account adopts the data collected before accounts existed
    # (DEFAULT_CUSTOMER_ID), so an upgraded single-user install keeps its scans.
    first = session.scalars(select(UserAccount).limit(1)).first() is None
    user = UserAccount(
        email=email,
        display_name=(display_name or "").strip()[:200] or email.split("@")[0],
        password_hash=hash_password(password) if microsoft is None else "",
        customer_id=config.DEFAULT_CUSTOMER_ID if first else secrets.token_hex(16),
        role=ROLE_ADMIN,  # who registers an organisation administers it
        email_verified=0 if config.SMTP_HOST and microsoft is None else 1,
        ms_oid=microsoft[0] if microsoft else "",
        ms_tid=microsoft[1] if microsoft else "",
    )
    session.add(user)
    session.commit()
    # Imported here: entitlements depends on deps, which depends on this module.
    from . import entitlements

    # Every registration is a new organisation on a fresh trial.
    entitlements.open_organisation(
        session,
        user.customer_id,
        (organisation or "").strip()[:200] or email.split("@")[1],
    )
    operator = entitlements.grant_platform_admin(session, user)
    if not operator and not first:
        from . import mailer
        from .models import Customer

        customer = session.get(Customer, user.customer_id)
        mailer.notify_signup(
            customer.name if customer else "",
            email,
            session.scalar(select(func.count()).select_from(Customer)) or 0,
        )
    return user


# --- members & invitations -------------------------------------------------
def admin_count(session: Session, customer_id: str, *, pending: bool = True) -> int:
    """Active admins, plus unexpired admin invitations: both hold a seat."""
    from sqlalchemy import func

    admins = session.scalar(
        select(func.count())
        .select_from(UserAccount)
        .where(
            UserAccount.customer_id == customer_id,
            UserAccount.role == ROLE_ADMIN,
            UserAccount.is_active == 1,
        )
    )
    if not pending:
        return admins or 0
    invited = session.scalar(
        select(func.count())
        .select_from(Invitation)
        .where(
            Invitation.customer_id == customer_id,
            Invitation.role == ROLE_ADMIN,
            Invitation.accepted_at.is_(None),
            Invitation.expires_at > utcnow(),
        )
    )
    return (admins or 0) + (invited or 0)


def _check_role(role: str) -> str:
    """The role, with old names mapped (user -> viewer), or AuthError."""
    role = LEGACY_ROLES.get(role, role)
    if role not in ROLES:
        raise AuthError(f"Role must be one of {', '.join(ROLES)}.")
    return role


def invite(
    session: Session, inviter: UserAccount, email: str, role: str
) -> tuple[Invitation, str]:
    """Create an invitation; returns it and the raw link token (shown once)."""
    email = normalise_email(email)
    role = _check_role(role)
    if session.scalars(select(UserAccount).where(UserAccount.email == email)).first():
        raise AuthError("That e-mail address already has an account.")
    if role == ROLE_ADMIN and admin_count(session, inviter.customer_id) >= MAX_ADMINS:
        raise AuthError(
            f"An organisation can have at most {MAX_ADMINS} admins (pending invitations "
            "count). Invite as a contributor or viewer, or demote an admin first."
        )
    # A fresh invitation replaces an older one for the same address.
    session.execute(
        delete(Invitation).where(
            Invitation.customer_id == inviter.customer_id,
            Invitation.email == email,
            Invitation.accepted_at.is_(None),
        )
    )
    from . import entitlements

    # After the replacement: re-inviting someone must not need a spare seat.
    entitlements.check_can_add_user(session, inviter.customer_id)
    token = secrets.token_urlsafe(32)
    row = Invitation(
        id=_digest(token),
        customer_id=inviter.customer_id,
        email=email,
        role=role,
        invited_by=inviter.id,
        expires_at=utcnow() + dt.timedelta(days=INVITATION_DAYS),
    )
    session.add(row)
    session.commit()
    return row, token


def open_invitation(session: Session, token: str) -> Invitation:
    row = session.get(Invitation, _digest(token or ""))
    if row is None or row.accepted_at or row.expires_at < utcnow():
        raise AuthError("This invitation is invalid, used or expired. Ask for a new one.")
    return row


def accept_invitation(
    session: Session, token: str, password: str, display_name: str = ""
) -> UserAccount:
    """
    Create the invited account inside the inviter's organisation.

    Works even when public registration is closed — the invitation is the
    permission. The address counts as verified: the link only reached it.
    """
    row = open_invitation(session, token)
    check_password_policy(password)
    if session.scalars(select(UserAccount).where(UserAccount.email == row.email)).first():
        raise AuthError("That e-mail address already has an account. Sign in instead.")
    role = row.role
    # The pending invitation held its admin seat; re-check without it in case
    # admins were promoted meanwhile.
    if (
        role == ROLE_ADMIN
        and admin_count(session, row.customer_id, pending=False) >= MAX_ADMINS
    ):
        raise AuthError(f"This organisation already has {MAX_ADMINS} admins.")
    from . import entitlements

    # The invitation held a seat; a downgrade since then may have taken it.
    entitlements.check_can_add_user(session, row.customer_id, pending=False)
    user = UserAccount(
        email=row.email,
        display_name=(display_name or "").strip()[:200] or row.email.split("@")[0],
        password_hash=hash_password(password),
        customer_id=row.customer_id,
        role=role,
        email_verified=1,
    )
    session.add(user)
    row.accepted_at = utcnow()
    session.commit()
    return user


def set_role(session: Session, actor: UserAccount, member: UserAccount, role: str) -> None:
    role = _check_role(role)
    if member.customer_id != actor.customer_id:
        raise AuthError("No such member.")
    if member.role == role:
        return
    if role == ROLE_ADMIN and admin_count(session, actor.customer_id) >= MAX_ADMINS:
        raise AuthError(f"An organisation can have at most {MAX_ADMINS} admins.")
    if (
        member.role == ROLE_ADMIN
        and admin_count(session, actor.customer_id, pending=False) <= 1
    ):
        raise AuthError("The organisation needs at least one admin.")
    member.role = role
    session.commit()


def remove_member(session: Session, actor: UserAccount, member: UserAccount) -> None:
    if member.customer_id != actor.customer_id:
        raise AuthError("No such member.")
    if (
        member.role == ROLE_ADMIN
        and admin_count(session, actor.customer_id, pending=False) <= 1
    ):
        raise AuthError("The organisation needs at least one admin.")
    session.execute(delete(UserSession).where(UserSession.user_id == member.id))
    session.delete(member)
    session.commit()


def user_for_microsoft(session: Session, oid: str, tid: str) -> UserAccount | None:
    """The account linked to this Microsoft identity, if any."""
    return session.scalars(
        select(UserAccount).where(UserAccount.ms_oid == oid, UserAccount.ms_tid == tid)
    ).first()


def authenticate(session: Session, email: str, password: str) -> UserAccount:
    try:
        email = normalise_email(email)
    except AuthError:
        raise AuthError("Wrong e-mail or password.") from None
    user = session.scalars(select(UserAccount).where(UserAccount.email == email)).first()
    # Hash even when the account does not exist, so timing does not reveal it.
    stored = user.password_hash if user else hash_password("timing-equaliser-Aa1")
    if not verify_password(password, stored) or user is None or not user.is_active:
        raise AuthError("Wrong e-mail or password.")
    if not user.email_verified:
        raise Unverified("Confirm your e-mail address first — check your inbox.")
    return user


# --- throttling ------------------------------------------------------------
class Throttle:
    """
    At most `limit` attempts per key per `window` seconds, in memory.

    Enough for one app instance; behind several, put a WAF rate rule in front
    (see DEPLOY.md). Keys are the client IP and the e-mail, so neither one host
    nor one targeted account can be hammered.
    """

    def __init__(self, limit: int = 10, window: int = 300):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                hits = self._hits[key]
                while hits and now - hits[0] > self.window:
                    hits.popleft()
                if len(hits) >= self.limit:
                    raise AuthError("Too many attempts. Wait a few minutes and try again.")
            for key in keys:
                self._hits[key].append(now)


login_throttle = Throttle(limit=10, window=300)
