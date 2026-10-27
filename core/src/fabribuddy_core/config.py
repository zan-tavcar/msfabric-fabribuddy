"""
Settings, all from the environment (optionally via backend/.env).

Nothing here holds a secret value — only the names of places secrets live.
Env vars are prefixed FL_ so this product's configuration never collides with
a FabriBuddy deployment on the same host.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/

load_dotenv(BASE_DIR / ".env")


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _path(name: str, default: str) -> str:
    raw = os.environ.get(name, default)
    p = Path(raw)
    return str(p if p.is_absolute() else BASE_DIR / p)


# --- storage ---------------------------------------------------------------
DB_URL = os.environ.get("FL_DB_URL") or f"sqlite:///{_path('FL_DB_FILE', 'lineage.db')}"

# 'file' -> LocalFileSecretStore, 'env' -> EnvSecretStore. Key Vault later.
SECRET_STORE = os.environ.get("FL_SECRET_STORE", "file").strip().lower()
SECRET_FILE = _path("FL_SECRET_FILE", ".secrets.json")

# --- tenancy ---------------------------------------------------------------
# Stand-in for the paying customer until real accounts exist. Every row carries
# one, so adding accounts later is wiring rather than a schema migration.
DEFAULT_CUSTOMER_ID = os.environ.get("FL_CUSTOMER_ID", "local-customer")

# Which tenant the unprefixed routes serve when more than one is connected.
DEFAULT_TENANT_ID = os.environ.get("FL_DEFAULT_TENANT_ID") or None

# --- demo ------------------------------------------------------------------
# Seeds the canonical Sales Data [Prod] / Sales BI [Prod] scenario at startup
# so the whole UI is walkable without a Fabric tenant.
DEMO_MODE = _flag("FL_DEMO_MODE", False)
# Column-level lineage is parked: lineage is table-level. The parsers still
# produce column edges (they are tested); the resolver stores none unless this
# is on.
COLUMN_LINEAGE = _flag("FL_COLUMN_LINEAGE", False)
DEMO_TENANT_NAME = "Demo tenant"

# --- bootstrap -------------------------------------------------------------
BOOTSTRAP_FROM_ENV = _flag("FL_BOOTSTRAP_FROM_ENV", True)

# --- collectors ------------------------------------------------------------
# Bounded concurrency: the Fabric and Power BI admin APIs rate-limit per
# tenant, so more workers only make collectors queue behind each other.
MAX_COLLECTOR_WORKERS = int(os.environ.get("FL_MAX_COLLECTOR_WORKERS", "4"))

# --- accounts & sessions ---------------------------------------------------
# Off only for local development and the test suite: every request then acts as
# DEFAULT_CUSTOMER_ID, exactly as before accounts existed.
AUTH_REQUIRED = _flag("FL_AUTH_REQUIRED", True)
ALLOW_REGISTRATION = _flag("FL_ALLOW_REGISTRATION", True)
SESSION_COOKIE = os.environ.get("FL_SESSION_COOKIE", "fl_session")
SESSION_DAYS = int(os.environ.get("FL_SESSION_DAYS", "14"))
# Secure cookies need HTTPS; on by default, off for http://localhost dev.
COOKIE_SECURE = _flag("FL_COOKIE_SECURE", True)
# Public origin, for links in e-mails (verification, password reset).
PUBLIC_URL = os.environ.get("FL_PUBLIC_URL", "http://localhost:5173").rstrip("/")

# Optional SMTP. When set, new accounts must verify their e-mail and password
# reset works by e-mail; when unset, registration signs straight in.
SMTP_HOST = os.environ.get("FL_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("FL_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("FL_SMTP_USER", "")
SMTP_PASSWORD_ENV = "FL_SMTP_PASSWORD"  # noqa: S105 — the variable NAME, read at send time
SMTP_FROM = os.environ.get("FL_SMTP_FROM", "FabriBuddy <no-reply@localhost>")

# "Continue with Microsoft" (signing in to FabriBuddy, never scanning):
# FabriBuddy's own multi-tenant Entra app, scripts/entra_signin_app.sh.
MS_CLIENT_ID = (os.environ.get("FL_MS_CLIENT_ID") or "").strip().lower()
MS_CLIENT_SECRET_ENV = "FL_MS_CLIENT_SECRET"  # noqa: S105 — the variable NAME


def ms_client_secret() -> str:
    return os.environ.get(MS_CLIENT_SECRET_ENV, "")


def user_signin_available() -> bool:
    return bool(MS_CLIENT_ID and ms_client_secret())


# Shown on the sign-in and sign-up screens only when turned on. Off by default:
# until the Entra app is publisher-verified, many tenants answer "Approval
# required" there, a poor first step.
MS_ACCOUNT_SIGNIN = _flag("FL_MS_ACCOUNT_SIGNIN", False)


def account_signin_available() -> bool:
    return MS_ACCOUNT_SIGNIN and user_signin_available()


def ms_account_redirect_uri() -> str:
    """Where Microsoft returns after signing in to FabriBuddy itself (public)."""
    return f"{PUBLIC_URL}/api/auth/microsoft/callback"


# Key for the 'encrypted' secret store (Fernet, urlsafe base64 32 bytes).
# Required in production; generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
SECRET_KEY_ENV = "FL_SECRET_KEY"  # noqa: S105 — the variable NAME, not a key

# --- billing & control plane -----------------------------------------------
# The operator's own accounts, comma-separated: at startup (and when one
# registers) its organisation is flagged platform admin, and only these exact
# accounts open /api/platform. No API can grant it.
PLATFORM_ADMIN_EMAIL = (os.environ.get("FL_PLATFORM_ADMIN_EMAIL") or "").strip().lower()


def platform_admins() -> set[str]:
    """The operator accounts named by FL_PLATFORM_ADMIN_EMAIL."""
    return {e.strip() for e in PLATFORM_ADMIN_EMAIL.lower().split(",") if e.strip()}


# Who hears about each new organisation (needs SMTP). Defaults to the first
# operator account; set it empty to turn the notice off.
SIGNUP_NOTIFY_EMAIL = (
    os.environ.get("FL_SIGNUP_NOTIFY_EMAIL", PLATFORM_ADMIN_EMAIL.split(",")[0]) or ""
).strip()
# The address customers are told to write to (plan requests, support).
CONTACT_EMAIL = (os.environ.get("FL_CONTACT_EMAIL") or "info@fabribuddy.com").strip()
# The operator console's host (e.g. admin.fabribuddy.com). When set, that host
# serves only the console (console.html + sign-in + /api/platform), and
# /api/platform answers 404 on every other host. Unset (local dev): the console
# is at /console.html on any host, still operator-only.
CONSOLE_HOST = (os.environ.get("FL_CONSOLE_HOST") or "").strip().lower()

# The Delta log step: "fast" (default) reads each table's last write and row
# count; "off" skips it — losing last-write times and the stale-table finding,
# row counts, Delta column descriptions, and writer attribution from the log.
DELTA_SCAN = (os.environ.get("FL_DELTA_SCAN") or "fast").strip().lower()

# A public, read-only demo (the website's "Live demo" link): with FL_DEMO_MODE=1
# and FL_AUTH_REQUIRED=0, anyone who opens it is the local operator — fine on a
# laptop, not on the internet. This turns every change away and hides settings,
# members and the console, so a visitor can look at everything and alter nothing.
PUBLIC_DEMO = os.environ.get("FL_PUBLIC_DEMO", "0") == "1"
# Where the demo's "Start a free trial" goes (the real app's sign-up).
DEMO_SIGNUP_URL = (os.environ.get("FL_DEMO_SIGNUP_URL") or "").strip()
# The demo organisation at /demo, inside a normal instance (app/demo_org.py).
DEMO_ORG = _flag("FL_DEMO_ORG", False)

# --- scheduler -------------------------------------------------------------
# In-process scheduler for the per-tenant scan times set on the Admin page.
# Run exactly one app instance with it on (see DEPLOY.md).
SCHEDULER_ENABLED = _flag("FL_SCHEDULER", True)
MAX_DAILY_SCANS = 8

# --- static frontend -------------------------------------------------------
# The built SPA (frontend/dist). Served by this app in production so the API
# and UI share one origin — no CORS, and cookies just work.
STATIC_DIR = os.environ.get("FL_STATIC_DIR", str(BASE_DIR.parent / "frontend" / "dist"))

# --- http ------------------------------------------------------------------
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "FL_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if o.strip()
]

# Stripe (optional). All three set = Subscribe goes through Checkout and only
# the signed webhook changes a plan; unset = Subscribe activates directly.
STRIPE_SECRET_KEY = (os.environ.get("FL_STRIPE_SECRET_KEY") or "").strip()
# One recurring monthly price per plan (Team, Business, Enterprise); a plan
# with no price cannot be bought through Checkout.
STRIPE_PRICE_IDS = {
    plan: (os.environ.get(f"FL_STRIPE_PRICE_ID_{plan.upper()}") or "").strip()
    for plan in ("team", "business", "enterprise")
}
STRIPE_WEBHOOK_SECRET = (os.environ.get("FL_STRIPE_WEBHOOK_SECRET") or "").strip()

# Paddle (optional; preferred). A merchant of record: Paddle sells in its own
# name, so it charges and remits VAT / sales tax and issues the invoices — the
# right fit for a seller in the EU without a tax department. Same model as
# Stripe above: only its signed webhook changes a subscription.
PADDLE_API_KEY = (os.environ.get("FL_PADDLE_API_KEY") or "").strip()
# Client-side token for Paddle.js (starts `live_` or `test_`); safe in a browser.
PADDLE_CLIENT_TOKEN = (os.environ.get("FL_PADDLE_CLIENT_TOKEN") or "").strip()
PADDLE_WEBHOOK_SECRET = (os.environ.get("FL_PADDLE_WEBHOOK_SECRET") or "").strip()
# sandbox | production
PADDLE_ENVIRONMENT = (os.environ.get("FL_PADDLE_ENVIRONMENT") or "sandbox").strip().lower()
PADDLE_PRICE_IDS = {
    plan: (os.environ.get(f"FL_PADDLE_PRICE_ID_{plan.upper()}") or "").strip()
    for plan in ("team", "business", "enterprise")
}

# Which provider takes payments: paddle | stripe | manual. Unset = Paddle when
# its key is set, else Stripe when its key is set, else manual (the choice
# applies directly and you invoice outside the product).
BILLING_PROVIDER_SETTING = (os.environ.get("FL_BILLING_PROVIDER") or "").strip().lower()


def billing_provider() -> str:
    """paddle | stripe | manual — evaluated on use, so a changed key applies."""
    if BILLING_PROVIDER_SETTING in ("paddle", "stripe", "manual"):
        return BILLING_PROVIDER_SETTING
    if PADDLE_API_KEY:
        return "paddle"
    if STRIPE_SECRET_KEY:
        return "stripe"
    return "manual"
