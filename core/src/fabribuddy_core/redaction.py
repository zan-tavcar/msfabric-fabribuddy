"""Keep credentials out of anything we store, log or return."""

import re

# Bearer tokens in an upstream error body, and the classic "client_secret=…"
# form that a failed token request echoes back.
_PATTERNS = [
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"(?i)(client_secret=)[^&\s\"']+"),
    re.compile(r"(?i)(\"?client_secret\"?\s*[:=]\s*\"?)[^,&\s\"']+"),
]


def scrub(text: str, secret: str | None = None) -> str:
    """
    Remove the tenant's own secret and any token-shaped string.

    The secret is passed in explicitly rather than searched for heuristically:
    a short or low-entropy secret would never match a generic pattern, and
    those are exactly the ones that end up in a support ticket.
    """
    if not text:
        return ""
    out = text
    if secret:
        out = out.replace(secret, "***")
    for pattern in _PATTERNS:
        out = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "***", out)
    return out
