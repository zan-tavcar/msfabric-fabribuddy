"""
How a stored answer is keyed and packed — the contract with the Fabric App.

The app asks for `/lineage/catalog`, `/lineage/nodes/<id>` and so on exactly
as the SaaS frontend asks its backend. The notebook stores each answer under a
key made from that request, and the app reads it back by the same key
(`app/src/fabric/payloads.ts` implements the other half; change both together).

- **Key:** the path (percent-decoded) plus the query parameters, decoded,
  sorted, `k=v` joined with `&`; then SHA-256 in hex. Hashing keeps the key a
  fixed 64 characters whatever an id looks like.
- **Body:** the JSON answer, UTF-8, gzip, base64, cut into chunks of at most
  4,000 characters (an `NVARCHAR(4000)` column: Fabric Apps' generated API
  handles bounded text columns, not `NVARCHAR(MAX)`).
"""

import base64
import gzip
import hashlib
import json
from urllib.parse import parse_qsl, unquote

CHUNK = 4000


def normalize(url: str) -> str:
    path, _, query = url.partition("?")
    pairs = sorted(parse_qsl(query, keep_blank_values=False))
    canonical = unquote(path)
    if pairs:
        canonical += "?" + "&".join(f"{k}={v}" for k, v in pairs)
    return canonical


def key(url: str) -> str:
    return hashlib.sha256(normalize(url).encode("utf-8")).hexdigest()


def pack(value) -> list[str]:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    # mtime=0: the same answer packs to the same bytes, so reruns are comparable.
    encoded = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    return [encoded[i : i + CHUNK] for i in range(0, len(encoded), CHUNK)] or [""]


def unpack(chunks: list[str]):
    return json.loads(gzip.decompress(base64.b64decode("".join(chunks))).decode("utf-8"))
