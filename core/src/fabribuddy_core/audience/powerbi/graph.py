"""
Microsoft Graph client for license data. Separate from the Power BI scanner
because it uses a different resource (graph.microsoft.com) and needs its own
SPN permissions: User.Read.All + Organization.Read.All (application,
admin-consented). Degrades gracefully if those aren't granted.
"""

import requests

from .credentials import Credentials

try:
    import msal
except ImportError as e:
    raise SystemExit("Missing dependency. Run: pip install -r requirements.txt") from e

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

# Power BI license SKU part numbers -> friendly names
PBI_SKUS = {
    "POWER_BI_PRO": "Pro",
    "POWER_BI_PRO_CE": "Pro",
    "POWER_BI_PRO_DEPT": "Pro",
    "PBI_PREMIUM_PER_USER": "PPU",
    "PBI_PREMIUM_PER_USER_ADDON": "PPU",
    "POWER_BI_STANDARD": "Free",
}


class GraphUnavailable(Exception):
    """Raised when Graph can't be used (no creds, no permission, etc.)."""


def _token(creds: Credentials):
    try:
        app = msal.ConfidentialClientApplication(
            creds.client_id,
            authority=f"https://login.microsoftonline.com/{creds.tenant_id}",
            client_credential=creds.client_secret,
        )
        res = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
    except (ValueError, requests.RequestException) as e:
        raise GraphUnavailable(f"Graph sign-in failed: {str(e)[:200]}") from None
    if "access_token" not in res:
        raise GraphUnavailable(
            f"Graph token failed: {res.get('error')} — {res.get('error_description', '')[:160]}"
        )
    return res["access_token"]


def _sku_map(token):
    """subscribedSkus: skuId -> skuPartNumber, so we can name each plan."""
    r = requests.get(
        f"{GRAPH}/subscribedSkus", headers={"Authorization": f"Bearer {token}"}, timeout=60
    )
    if r.status_code == 403:
        raise GraphUnavailable(
            "Graph permission missing. Grant the SPN Organization.Read.All + "
            "User.Read.All (application) with admin consent to enable license data."
        )
    if r.status_code != 200:
        raise GraphUnavailable(f"subscribedSkus returned {r.status_code}: {r.text[:200]}")
    out = {}
    for s in r.json().get("value", []):
        out[s.get("skuId")] = s.get("skuPartNumber", "")
    return out


def fetch_group_members(creds: Credentials, group_ids, page_size=999):
    """
    Expand Entra security groups to the individual people inside them.

    Uses transitiveMembers, so a user in a nested group is still resolved —
    Power BI honours nesting, and reporting only direct members would
    understate who can actually see a report.

    Returns {groupId: [{name, upn, id, type}]}. A group that cannot be read is
    recorded as an error rather than silently returned empty, because "no
    members" and "we could not look" mean very different things for access.
    """
    token = _token(creds)
    headers = {"Authorization": f"Bearer {token}"}
    out, errors = {}, {}

    for gid in group_ids:
        if not gid:
            continue
        members = []
        url = (
            f"{GRAPH}/groups/{gid}/transitiveMembers"
            f"?$select=id,displayName,userPrincipalName,mail&$top={page_size}"
        )
        failed = False
        try:
            while url:
                r = requests.get(url, headers=headers, timeout=60)
                if r.status_code != 200:
                    errors[gid] = f"{r.status_code}: {r.text[:120]}"
                    failed = True
                    break
                body = r.json()
                for m in body.get("value", []):
                    kind = (m.get("@odata.type") or "").rsplit(".", 1)[-1]
                    if kind == "group":
                        continue  # transitiveMembers already flattens nesting
                    members.append(
                        {
                            "id": m.get("id") or "",
                            "name": m.get("displayName")
                            or m.get("userPrincipalName")
                            or "",
                            "upn": (
                                m.get("userPrincipalName") or m.get("mail") or ""
                            ).lower(),
                            "type": "User",
                        }
                    )
                url = body.get("@odata.nextLink")
        except requests.RequestException as e:
            errors[gid] = str(e)[:120]
            failed = True

        if not failed:
            out[gid] = members

    return {"members": out, "errors": errors}


def probe(creds: Credentials):
    """
    Cheap capability check for the connect-tenant flow: can this SPN read
    licence data at all? Raises GraphUnavailable with the actionable reason
    if not. Graph is optional — a tenant without it still connects, just
    without the unused-seat panel.
    """
    _sku_map(_token(creds))
    return True


def fetch_licenses(creds: Credentials):
    """
    Returns list of users with a Power BI license (Pro/PPU/Free), or raises
    GraphUnavailable. Does NOT determine 'unused' — that needs activity data,
    joined later in services/scans.py.
    """
    token = _token(creds)
    sku_by_id = _sku_map(token)

    users = []
    url = f"{GRAPH}/users?$select=displayName,userPrincipalName,assignedLicenses&$top=200"
    headers = {"Authorization": f"Bearer {token}"}
    while url:
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code == 403:
            raise GraphUnavailable(
                "Graph permission missing. Grant the SPN User.Read.All "
                "(application) with admin consent to enable license data."
            )
        if r.status_code != 200:
            raise GraphUnavailable(f"/users returned {r.status_code}: {r.text[:200]}")
        body = r.json()
        for u in body.get("value", []):
            plans = []
            for lic in u.get("assignedLicenses", []):
                part = sku_by_id.get(lic.get("skuId"), "")
                friendly = PBI_SKUS.get(part)
                if friendly and friendly != "Free":
                    plans.append(friendly)
            if plans:
                # PPU supersedes Pro if both present
                plan = "PPU" if "PPU" in plans else "Pro"
                users.append(
                    {
                        "name": u.get("displayName") or u.get("userPrincipalName"),
                        "upn": (u.get("userPrincipalName") or "").lower(),
                        "license": plan,
                    }
                )
        url = body.get("@odata.nextLink")
    return users
