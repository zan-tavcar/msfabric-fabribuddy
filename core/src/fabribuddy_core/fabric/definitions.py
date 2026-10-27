"""
Item definitions via `getDefinition` (a long-running operation).

Contributor tier. Worth restating because it is the most invasive thing this
product asks for: the API requires the caller to hold *read and write*
permission on the item (scope `Item.ReadWrite.All`), even though the operation
only reads. Nothing here ever writes — `allow_write=True` on the request is
about the HTTP verb being POST, not about mutating the tenant.

Per item type, the parts that matter:
  Notebook       notebook-content.py
  DataPipeline   pipeline-content.json
  Dataflow       mashup.pq, queryMetadata.json
  SemanticModel  definition/**/*.tmdl
  Report         definition/**/visual.json, definition/**/page.json
  VariableLibrary variables.json, valueSets/*.json
  MirroredDatabase mirroring.json
  Eventstream    eventstream.json
"""

import base64
import binascii
import logging

from .client import API_FABRIC, SCOPE_FABRIC, AccessDenied, FabricClient, FabricError

log = logging.getLogger("lineage.definitions")

# `format` values Fabric needs for the types that have more than one shape.
FORMATS = {
    "Notebook": "ipynb",  # only when we want .ipynb; default gives the .py
    "SemanticModel": "TMDL",
    "Report": "PBIR",
}

# Which parts each item type's parsers care about. Everything else (platform
# files, images, large binary blobs) is skipped so a definition fetch does not
# drag a report's assets into the database.
RELEVANT_PARTS = {
    "Notebook": ("notebook-content.py",),
    "DataPipeline": ("pipeline-content.json",),
    "Dataflow": ("mashup.pq", "querymetadata.json"),
    "SemanticModel": (".tmdl",),
    "Report": ("visual.json", "page.json", "report.json", "definition.pbir"),
    # Variable Library: default values plus one file per value set. The active
    # set is not in the definition — see get_active_value_set.
    "VariableLibrary": ("variables.json", "valuesets/", "valueset/", "settings.json"),
    "MirroredDatabase": ("mirroring.json",),
    "Eventstream": ("eventstream.json",),
}

LANGUAGES = {
    ".py": "python",
    ".json": "json",
    ".pq": "m",
    ".tmdl": "tmdl",
    ".sql": "sql",
    ".pbir": "json",
}


def language_for(path: str) -> str:
    lowered = path.lower()
    for suffix, language in LANGUAGES.items():
        if lowered.endswith(suffix):
            return language
    return "text"


def is_relevant(item_type: str, path: str) -> bool:
    markers = RELEVANT_PARTS.get(item_type)
    if markers is None:
        return False
    lowered = path.lower()
    return any(marker in lowered for marker in markers)


def decode_part(part: dict) -> str | None:
    """
    Base64 part -> text.

    Returns None for anything that is not decodable UTF-8: a report's embedded
    PNG is a legitimate part we have no use for, and it must not abort the item.
    """
    payload = part.get("payload") or ""
    if part.get("payloadType") not in (None, "", "InlineBase64"):
        return None
    try:
        raw = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def get_definition(
    client: FabricClient, workspace_guid: str, item_guid: str, item_type: str = ""
) -> dict[str, str]:
    """
    Fetch one item's definition and return {part path: text}.

    Raises AccessDenied when the SPN lacks Contributor or the item carries a
    protected sensitivity label — the caller turns that into a per-item error
    with the right explanation rather than failing the run.
    """
    url = f"{API_FABRIC}/workspaces/{workspace_guid}/items/{item_guid}/getDefinition"
    params = {}
    if item_type in ("SemanticModel", "Report"):
        params["format"] = FORMATS[item_type]

    response = client.request("POST", url, SCOPE_FABRIC, params=params, allow_write=True)
    payload = client.poll_lro(response, SCOPE_FABRIC)

    parts = (payload.get("definition") or {}).get("parts") or []
    out: dict[str, str] = {}
    for part in parts:
        path = part.get("path") or ""
        if not path or (item_type and not is_relevant(item_type, path)):
            continue
        text = decode_part(part)
        if text is not None:
            out[path] = text
    return out


def classify_denial(error: Exception, sensitivity_label: str = "") -> str:
    """
    Why a definition fetch failed, in a form the UI can act on.

    A protected label and a missing workspace role produce the same 403, so the
    label we already hold from the Scanner API is what tells them apart.
    """
    if isinstance(error, AccessDenied):
        return "protected_label" if sensitivity_label else "needs_contributor"
    if isinstance(error, FabricError) and "rate" in str(error).lower():
        return "rate_limited"
    return "error"


def get_active_value_set(
    client: FabricClient, workspace_guid: str, library_guid: str
) -> str:
    """
    The value set a Variable Library currently resolves to.

    Not part of the definition: it is a property of the item
    (`GET /workspaces/{ws}/variableLibraries/{id}` -> properties.activeValueSetName),
    and it differs per workspace — dev and prod share a definition and differ
    only here.
    """
    payload = client.get_json(
        f"{API_FABRIC}/workspaces/{workspace_guid}/variableLibraries/{library_guid}",
        SCOPE_FABRIC,
    )
    return str((payload.get("properties") or {}).get("activeValueSetName") or "")
