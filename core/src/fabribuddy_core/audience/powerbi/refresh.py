"""
Semantic-model refresh state via /admin/capacities/refreshables.

This is the only admin API that returns a refresh *status* (Completed/Failed)
alongside a timestamp — the audit log tells you a refresh happened, not whether
it worked. Only datasets on dedicated capacity with recent history are
covered, so callers must treat a missing entry as "unknown", never as "fine".
"""

from .credentials import Credentials
from .scanner import ScanError, _get, get_token


def fetch_refreshables(creds: Credentials, top=5000):
    """
    Returns {datasetId: {...}} for every refreshable the tenant exposes.
    Never raises — refresh state is a nice-to-have, not a reason to fail a scan.
    """
    try:
        token = get_token(creds)
        data = _get(
            token,
            "/admin/capacities/refreshables",
            {"$top": top, "$expand": "capacity,group"},
        )
    except ScanError as e:
        return {"available": False, "reason": str(e), "items": {}}

    items = {}
    failing = 0
    with_status = 0

    for v in data.get("value", []):
        if v.get("kind") != "Dataset":
            continue
        last = v.get("lastRefresh") or {}
        status = last.get("status") or ""
        if status:
            with_status += 1
        if status and status.lower() not in ("completed", "unknown"):
            failing += 1

        items[v.get("id")] = {
            "name": v.get("name") or "",
            "status": status,
            "endTime": last.get("endTime") or last.get("startTime") or "",
            "refreshType": last.get("refreshType") or "",
            "refreshCount": v.get("refreshCount"),
            "refreshFailures": v.get("refreshFailures"),
            "scheduled": bool((v.get("refreshSchedule") or {}).get("enabled")),
            "workspace": (v.get("group") or {}).get("name") or "",
            "capacity": (v.get("capacity") or {}).get("displayName") or "",
        }

    return {
        "available": True,
        "items": items,
        "summary": {
            "total": len(items),
            "withStatus": with_status,
            "failing": failing,
        },
    }
