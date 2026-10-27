"""
dbt-style node selectors.

The syntax people already know from `dbt run --select`:

    fact_sales        the node itself
    +fact_sales       it and everything upstream
    fact_sales+       it and everything downstream
    +fact_sales+      the whole path through it
    2+fact_sales      two hops upstream
    fact_sales+2      two hops downstream
    tag:pii           every node carrying a tag
    layer:gold        every node in a lane
    workspace:"Sales Data [Prod]"
    kind:lakehouse_table

Space- or comma-separated terms union together, which matches dbt's behaviour
and is what someone typing `layer:gold layer:semantic` expects.

Name matching is deliberately loose — a case-insensitive match against the
table name, the `schema.table` form, or the display path — because nobody is
going to type `lh:11111111-…:dbo.fact_sales` into a filter box.
"""

import re
import shlex

from .graph import Adjacency
from .models import DataNode

# `2+name+3` — optional hop counts on either side.
_TERM = re.compile(r"^(?P<up_n>\d*)(?P<up>\+?)(?P<body>.*?)(?P<down>\+?)(?P<down_n>\d*)$")

DEFAULT_HOPS = 25  # a bare '+' means "all the way"


class SelectorError(ValueError):
    """The selector could not be understood. Surfaced to the user verbatim."""


def split_terms(selector: str) -> list[str]:
    """
    Split on commas and whitespace, honouring quotes.

    shlex handles `workspace:"Sales Data [Prod]"` — a workspace name with spaces
    is the common case, so quoting has to work.
    """
    if not selector or not selector.strip():
        return []
    normalised = selector.replace(",", " ")
    try:
        return [t for t in shlex.split(normalised) if t]
    except ValueError as e:
        raise SelectorError(f"Unbalanced quotes in selector: {e}") from None


def _matches_name(node: DataNode, needle: str) -> bool:
    needle = needle.lower()
    candidates = [
        (node.table_name or "").lower(),
        f"{node.schema_name}.{node.table_name}".lower(),
        (node.display_path or "").lower(),
        node.id.lower(),
    ]
    return any(needle == c or needle in c for c in candidates)


def _select_body(body: str, nodes: list[DataNode], docs: dict) -> set[str]:
    """Resolve one term's body (no +) to a node id set."""
    if not body:
        # A bare '+' selects nothing on its own rather than everything: an empty
        # filter box should not be the same request as "the entire tenant".
        return set()

    prefix, _, value = body.partition(":")
    prefix = prefix.lower()
    value_lower = value.lower()

    if prefix == "layer" and value:
        return {n.id for n in nodes if n.layer == value_lower}
    if prefix == "kind" and value:
        return {n.id for n in nodes if n.kind == value_lower}
    if prefix == "workspace" and value:
        return {
            n.id
            for n in nodes
            if n.display_path and n.display_path.lower().startswith(value_lower)
        }
    if prefix == "tag" and value:
        out = set()
        for node in nodes:
            tags = {t.lower() for t in (docs.get(node.id) or [])}
            if value_lower in tags:
                out.add(node.id)
        return out
    if prefix == "id" and value:
        return {n.id for n in nodes if n.id == value}

    return {n.id for n in nodes if _matches_name(n, body)}


def resolve(
    selector: str,
    nodes: list[DataNode],
    adjacency: Adjacency,
    *,
    tags_by_node: dict[str, list[str]] | None = None,
) -> set[str]:
    """
    Apply a selector, returning the selected node ids.

    An empty selector selects everything — the graph view's default is the whole
    estate, and returning nothing there would render a blank canvas.
    """
    terms = split_terms(selector)
    if not terms:
        return {n.id for n in nodes}

    tags_by_node = tags_by_node or {}
    selected: set[str] = set()

    for term in terms:
        match = _TERM.match(term)
        if match is None:  # pragma: no cover — the regex accepts everything
            raise SelectorError(f"Could not parse selector term {term!r}.")

        body = match.group("body")
        seeds = _select_body(body, nodes, tags_by_node)
        selected |= seeds

        if match.group("up"):
            hops = int(match.group("up_n") or DEFAULT_HOPS)
            for seed in seeds:
                selected |= set(adjacency.walk(seed, "up", hops))
        if match.group("down"):
            hops = int(match.group("down_n") or DEFAULT_HOPS)
            for seed in seeds:
                selected |= set(adjacency.walk(seed, "down", hops))

    return selected
