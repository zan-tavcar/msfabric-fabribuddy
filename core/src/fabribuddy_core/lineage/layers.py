"""
Which lane a node belongs in: the storage it physically lives in.

External source → mirrored database → lakehouse → warehouse → semantic model →
report. The lane is a fact about the node (its kind), not an interpretation of
its name. Medallion naming is a convention a tenant may or may not follow;
where the data lives is always true.
"""

from .models import LAYER_BY_KIND, LAYER_LAKEHOUSE


def layer_for_kind(kind: str) -> str:
    return LAYER_BY_KIND.get(kind, LAYER_LAKEHOUSE)


def resolve(kind: str, *_name_parts: str) -> tuple[str, bool]:
    """
    Returns (layer, is_declared). Always declared: the kind decides.

    Name parts are accepted and ignored so callers that once passed
    `lakehouse, schema, table` for name rules need no change.
    """
    return layer_for_kind(kind), True
