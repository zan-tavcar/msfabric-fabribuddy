"""
One HTTP client for every Microsoft API this product talks to.

Three token audiences are in play and they are NOT interchangeable:

  SCOPE_FABRIC   api.fabric.microsoft.com   — items, definitions, job runs
  SCOPE_POWERBI  analysis.windows.net       — Scanner API and admin endpoints
  SCOPE_STORAGE  storage.azure.com          — OneLake, both the filesystem
                                              (dfs) and table endpoints

Everything else here exists because these APIs fail in specific, documented
ways: 429 with Retry-After, 202 + Location for long-running operations,
`continuationToken` paging, and transient 5xx mid-scan. Getting any of them
wrong silently truncates a scan, which is worse than failing it.

Read-only by contract: `request()` refuses any verb other than GET/HEAD unless
the caller passes allow_write=True, which only the handful of POST-shaped read
operations (getInfo, getDefinition) do. Nothing in this product ever creates,
updates or deletes in a customer tenant.
"""

import logging
import threading
import time

import requests

from .credentials import Credentials

log = logging.getLogger("lineage.fabric")

AUTHORITY_BASE = "https://login.microsoftonline.com"

SCOPE_FABRIC = "https://api.fabric.microsoft.com/.default"
SCOPE_POWERBI = "https://analysis.windows.net/powerbi/api/.default"
SCOPE_STORAGE = "https://storage.azure.com/.default"

API_FABRIC = "https://api.fabric.microsoft.com/v1"
API_POWERBI = "https://api.powerbi.com/v1.0/myorg"
API_ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"
API_ONELAKE_TABLE = "https://onelake.table.fabric.microsoft.com"

# POST endpoints that only ever read. Anything not on this list is refused.
_READ_ONLY_POSTS = ("/getdefinition", "/getinfo", "/admin/workspaces/getinfo")

NETWORK_RETRIES = 4
# Fabric returns 202 for getDefinition and other LROs. A notebook definition
# resolves in seconds; a large semantic model can take a couple of minutes.
LRO_TIMEOUT = 300
LRO_MIN_INTERVAL = 2


class FabricError(Exception):
    """An upstream call failed in a way the caller should report, not retry."""


class AccessDenied(FabricError):
    """
    401/403 — a permissions problem, not a transient one.

    Kept distinct because it is the single most common failure in this product
    and it always means the same thing to the customer: a tenant setting or a
    workspace role is missing. Retrying it is pointless.
    """


class RateLimited(FabricError):
    """429 that survived our retries. Carries the server's own Retry-After."""

    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = retry_after


class TokenProvider:
    """
    MSAL client-credentials tokens, cached per audience.

    MSAL keeps its own in-memory cache, but a ConfidentialClientApplication is
    not cheap to construct and the collectors ask for a token on every request,
    so the app object is built once per credential set and shared. The lock
    matters: collectors run on a thread pool and MSAL's cache is not reentrant.
    """

    def __init__(self, creds: Credentials):
        self._creds = creds
        self._app = None
        self._lock = threading.Lock()

    def _application(self):
        import msal

        if self._app is None:
            try:
                self._app = msal.ConfidentialClientApplication(
                    self._creds.client_id,
                    authority=f"{AUTHORITY_BASE}/{self._creds.tenant_id}",
                    client_credential=self._creds.client_secret,
                )
            except ValueError as e:
                # MSAL resolves the authority at construction, so a wrong
                # tenant id fails here — before any network call. Common when
                # someone pastes the client id into the tenant field.
                raise FabricError(
                    f"Could not reach the sign-in authority for tenant "
                    f"'{self._creds.tenant_id}'. Check the TenantId. "
                    f"({str(e)[:200]})"
                ) from None
        return self._app

    def token(self, scope: str) -> str:
        with self._lock:
            app = self._application()
            try:
                result = app.acquire_token_for_client(scopes=[scope])
            except requests.RequestException as e:
                raise FabricError(f"Network error acquiring a token: {e}") from None
        if "access_token" not in result:
            error = result.get("error", "unknown_error")
            description = str(result.get("error_description", ""))[:200]
            # AADSTS500011 = the SPN has no consent for this audience at all,
            # which is a different fix from a missing tenant setting.
            raise AccessDenied(
                f"Could not get a token for {scope}: {error} — {description}"
            )
        return result["access_token"]


class FabricClient:
    """
    Verb-and-audience aware HTTP with the retry behaviour these APIs need.

    Not a generic wrapper: every branch below corresponds to a failure that
    actually truncates a scan if you let it through.
    """

    def __init__(self, creds: Credentials, session: requests.Session | None = None):
        self.creds = creds
        self.tokens = TokenProvider(creds)
        # A shared Session gives connection pooling, which halves the wall time
        # of a per-item collector on a large tenant.
        self.session = session or requests.Session()

    # --- core ------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        scope: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict | None = None,
        timeout: int = 90,
        allow_write: bool = False,
        _attempt: int = 1,
    ) -> requests.Response:
        method = method.upper()
        if method not in ("GET", "HEAD") and not allow_write:
            raise FabricError(
                f"Refusing {method} {url}: this product is read-only against "
                f"customer tenants."
            )
        if method == "POST" and not any(
            marker in url.lower() for marker in _READ_ONLY_POSTS
        ):
            raise FabricError(f"Refusing POST {url}: not a known read-only operation.")

        request_headers = {"Authorization": f"Bearer {self.tokens.token(scope)}"}
        if json_body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})

        try:
            response = self.session.request(
                method,
                url,
                params=params or {},
                json=json_body,
                headers=request_headers,
                timeout=timeout,
            )
        except requests.RequestException as e:
            # Microsoft resets long-lived connections partway through a big
            # collection. This is not an HTTP status, so without the catch it
            # escapes as a 500 and discards the whole run.
            if _attempt <= NETWORK_RETRIES:
                time.sleep(min(2**_attempt, 20))
                return self.request(
                    method,
                    url,
                    scope,
                    params=params,
                    json_body=json_body,
                    headers=headers,
                    timeout=timeout,
                    allow_write=allow_write,
                    _attempt=_attempt + 1,
                )
            raise FabricError(
                f"Network error calling {url} after {NETWORK_RETRIES} retries: "
                f"{type(e).__name__}: {str(e)[:160]}"
            ) from None

        if response.status_code in (401, 403):
            raise AccessDenied(
                self._denial_message(url, response) + _microsoft_says(response)
            )

        if response.status_code == 429:
            wait = _retry_after(response, default=10)
            if _attempt <= NETWORK_RETRIES:
                log.info("429 from %s — waiting %ss", url, wait)
                time.sleep(wait)
                return self.request(
                    method,
                    url,
                    scope,
                    params=params,
                    json_body=json_body,
                    headers=headers,
                    timeout=timeout,
                    allow_write=allow_write,
                    _attempt=_attempt + 1,
                )
            raise RateLimited(
                f"Rate limited by {url} after {NETWORK_RETRIES} retries.", wait
            )

        if response.status_code >= 500 and _attempt <= NETWORK_RETRIES:
            time.sleep(min(2**_attempt, 20))
            return self.request(
                method,
                url,
                scope,
                params=params,
                json_body=json_body,
                headers=headers,
                timeout=timeout,
                allow_write=allow_write,
                _attempt=_attempt + 1,
            )

        return response

    def get_json(self, url: str, scope: str, **kwargs) -> dict:
        response = self.request("GET", url, scope, **kwargs)
        if response.status_code == 404:
            raise FabricError(f"{url} returned 404.")
        if response.status_code != 200:
            raise FabricError(
                f"{url} returned {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    # --- paging ------------------------------------------------------------
    def paged(self, url: str, scope: str, *, params: dict | None = None) -> list[dict]:
        """
        Follow `continuationToken` / `continuationUri` to the end.

        A hard page cap is deliberately absent: truncating an inventory makes
        every downstream count silently wrong, which is the failure mode this
        whole product exists to catch in other people's estates.
        """
        out: list[dict] = []
        params = dict(params or {})
        seen_tokens: set[str] = set()
        while True:
            sent = params.get("continuationToken")
            payload = self.get_json(url, scope, params=params)
            token = payload.get("continuationToken")

            # The service handed back the token we just sent, meaning it did
            # not advance: this page is a repeat of the last one. Break BEFORE
            # extending — appending it would silently duplicate rows in an
            # inventory, which is worse than stopping a page early.
            if sent is not None and token == sent:
                log.warning("paging stopped: %s echoed its continuation token", url)
                break

            out.extend(payload.get("value") or [])
            if not token:
                break
            if token in seen_tokens:
                # An A -> B -> A cycle rather than a stuck token.
                log.warning("paging stopped: %s revisited a continuation token", url)
                break
            seen_tokens.add(token)
            params["continuationToken"] = token
        return out

    # --- long-running operations -------------------------------------------
    def poll_lro(self, response: requests.Response, scope: str) -> dict:
        """
        Resolve a 202 into its result.

        Fabric's LRO shape: 202 + `Location` + `Retry-After`. Polling Location
        yields a status document; once it reports Succeeded the *result* lives
        at `<location>/result` (or at the `resultUrl` the status hands back).
        Honouring the server's Retry-After rather than a fixed sleep is what
        keeps a definition-heavy run inside the rate limit.
        """
        if response.status_code == 200:
            return response.json()
        if response.status_code != 202:
            raise FabricError(
                f"Expected 200 or 202, got {response.status_code}: {response.text[:300]}"
            )

        location = response.headers.get("Location")
        if not location:
            raise FabricError("Long-running operation returned 202 with no Location.")
        interval = max(_retry_after(response, default=LRO_MIN_INTERVAL), LRO_MIN_INTERVAL)
        deadline = time.monotonic() + LRO_TIMEOUT

        while time.monotonic() < deadline:
            time.sleep(interval)
            status = self.get_json(location, scope)
            state = (status.get("status") or "").lower()
            if state in ("succeeded", "completed"):
                result_url = status.get("resultUrl") or f"{location.rstrip('/')}/result"
                return self.get_json(result_url, scope)
            if state in ("failed", "cancelled"):
                error = (status.get("error") or {}).get("message") or state
                raise FabricError(f"Operation {state}: {error}")
            interval = max(
                _retry_after_value(status.get("retryAfter"), interval), LRO_MIN_INTERVAL
            )

        raise FabricError(f"Long-running operation did not finish within {LRO_TIMEOUT}s.")

    # --- messages ----------------------------------------------------------
    @staticmethod
    def _denial_message(url: str, response: requests.Response) -> str:
        """
        Turn a bare 403 into the actual fix.

        Which permission is missing depends entirely on which endpoint refused,
        and the customer cannot be expected to know the mapping — so we say it.
        """
        lowered = url.lower()
        if "admin/workspaces/getinfo" in lowered or "/admin/" in lowered:
            return (
                "Admin API access denied. In the Fabric admin portal, turn on "
                "'Service principals can use read-only admin APIs' and add this "
                "SPN's security group to it."
            )
        if "getdefinition" in lowered:
            return (
                "Item definition access denied. getDefinition requires the SPN "
                "to be Contributor on the workspace (the API demands read AND "
                "write permission on the item, even though nothing is written). "
                "Alternatively use Git mode for this workspace."
            )
        if "onelake" in lowered:
            return (
                "OneLake access denied. The SPN needs Contributor on the "
                "workspace, or an explicit OneLake data-access role granting "
                "read on this lakehouse."
            )
        return f"Access denied calling {url} ({response.status_code})."


def _microsoft_says(response: requests.Response) -> str:
    """
    Microsoft's own reason, appended to our advice: its error code is what tells
    "not in the security group" from "app has Power BI permissions" (`API is not
    accessible for application`) from a setting still propagating.
    """
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        detail = error.get("code") or ""
        message = error.get("message") or ""
        if message and message != detail:
            detail = f"{detail}: {message}" if detail else message
    else:
        detail = str(body.get("message") or body.get("Message") or "") if body else ""
        detail = detail or (response.text or "").strip()
    detail = " ".join(detail.split())[:240]
    return f" [HTTP {response.status_code}{': ' + detail if detail else ''}]"


def _retry_after(response: requests.Response, default: int) -> int:
    return _retry_after_value(response.headers.get("Retry-After"), default)


def _retry_after_value(raw, default: int) -> int:
    """Retry-After is seconds as a string, but services also send an int or null."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    # A malicious or broken upstream must not be able to park a worker for an
    # hour; cap it and let the caller retry the run instead.
    return max(1, min(value, 120))
