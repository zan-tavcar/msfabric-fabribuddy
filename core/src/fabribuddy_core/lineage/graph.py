"""
Graph traversal: upstream, downstream, and column-level tracing.

Everything is breadth-first with an explicit visited set. Lineage graphs
acquire cycles in practice — a MERGE that reads its own target, a notebook that
writes a table another notebook in the same run reads back — and a recursive
walk without one hangs the request rather than returning a slightly wrong
answer. Depth is bounded for the same reason: the graph view asks for 2 hops,
not the transitive closure of a 5,000-table estate.
"""

from collections import defaultdict, deque

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import repository
from .models import ColumnEdge, ColumnNode, DataNode, LineageEdge

# A traversal with no explicit limit still stops somewhere: past this many hops
# the answer is "everything", which no UI can draw usefully.
MAX_DEPTH = 25


class Adjacency:
    """
    Edges indexed both ways, built once per request.

    A node page needs upstream, downstream and the edges between them; doing
    that with per-node queries is a round trip per hop, which on a deep chain is
    slower than loading the tenant's edge list once.
    """

    def __init__(self, edges: list[LineageEdge]):
        self.edges = edges
        self.parents: dict[str, set[str]] = defaultdict(set)
        self.children: dict[str, set[str]] = defaultdict(set)
        # Several items can write the same target; keep them all so the UI can
        # say "written by nb_x and nb_y" rather than silently picking one.
        self.by_pair: dict[tuple[str, str], list[LineageEdge]] = defaultdict(list)
        for edge in edges:
            self.parents[edge.to_node_id].add(edge.from_node_id)
            self.children[edge.from_node_id].add(edge.to_node_id)
            self.by_pair[(edge.from_node_id, edge.to_node_id)].append(edge)

    def walk(self, start: str, direction: str, depth: int) -> dict[str, int]:
        """
        Reachable nodes and the hop count at which each was first reached.

        Returns the *shortest* hop count, which is what a depth limit should
        mean: "within 2 hops" must include a node reachable in 1, even if a
        longer path to it also exists.
        """
        step = self.parents if direction == "up" else self.children
        seen: dict[str, int] = {}
        queue = deque([(start, 0)])
        limit = min(max(depth, 0), MAX_DEPTH)
        while queue:
            node, hops = queue.popleft()
            if hops >= limit:
                continue
            for neighbour in step.get(node, ()):
                if neighbour in seen or neighbour == start:
                    continue
                seen[neighbour] = hops + 1
                queue.append((neighbour, hops + 1))
        return seen


def load_adjacency(session: Session, customer_id: str, tenant_id: str) -> Adjacency:
    """
    The live lineage: edges whose both ends are current nodes.

    A table a later scan no longer finds is soft-deleted, but its edges stay
    (history); followed, they draw boxes with nothing behind them and pages
    that answer Not Found.
    """
    from .models import DataNode

    gone = select(DataNode.id).where(
        DataNode.customer_id == customer_id,
        DataNode.tenant_id == tenant_id,
        DataNode.is_deleted.is_(True),
    )
    stmt = select(LineageEdge).where(
        LineageEdge.customer_id == customer_id,
        LineageEdge.tenant_id == tenant_id,
        LineageEdge.from_node_id.not_in(gone),
        LineageEdge.to_node_id.not_in(gone),
    )
    return Adjacency(list(session.scalars(stmt)))


def neighbourhood(
    adjacency: Adjacency, focus: str, up: int = 2, down: int = 2
) -> tuple[set[str], dict[str, int]]:
    """
    Nodes within `up` hops upstream and `down` downstream of `focus`.

    Returns the node set plus signed depths (negative upstream, positive
    downstream) so a layout can fall back to depth when a lane is unknown.
    """
    upstream = adjacency.walk(focus, "up", up)
    downstream = adjacency.walk(focus, "down", down)
    nodes = {focus} | set(upstream) | set(downstream)
    depths = {focus: 0}
    for node, hops in upstream.items():
        depths[node] = -hops
    for node, hops in downstream.items():
        # A node reachable both up and down (a cycle) keeps the upstream
        # reading: showing it left of the focus matches how it was found.
        depths.setdefault(node, hops)
    return nodes, depths


def direct(adjacency: Adjacency, node_id: str) -> tuple[list[str], list[str]]:
    """Immediate parents and children, for the Overview tab's two lists."""
    return (
        sorted(adjacency.parents.get(node_id, ())),
        sorted(adjacency.children.get(node_id, ())),
    )


def full_paths(adjacency: Adjacency, focus: str) -> set[str]:
    """
    Every node on any path through `focus`, unbounded.

    This is what the Lineage tab highlights in amber while dimming the rest, so
    it deliberately ignores the depth limits the graph view applies.
    """
    return (
        {focus}
        | set(adjacency.walk(focus, "up", MAX_DEPTH))
        | set(adjacency.walk(focus, "down", MAX_DEPTH))
    )


def edges_between(adjacency: Adjacency, nodes: set[str]) -> list[LineageEdge]:
    """Edges with both ends inside `nodes` — no dangling stubs in the render."""
    return [e for e in adjacency.edges if e.from_node_id in nodes and e.to_node_id in nodes]


# --- column level ----------------------------------------------------------
class ColumnAdjacency:
    def __init__(self, edges: list[ColumnEdge]):
        self.edges = edges
        self.parents: dict[str, set[str]] = defaultdict(set)
        self.children: dict[str, set[str]] = defaultdict(set)
        self.transform: dict[tuple[str, str], ColumnEdge] = {}
        for edge in edges:
            self.parents[edge.to_column_id].add(edge.from_column_id)
            self.children[edge.from_column_id].add(edge.to_column_id)
            self.transform[(edge.from_column_id, edge.to_column_id)] = edge

    def walk(self, start: str, direction: str) -> set[str]:
        step = self.parents if direction == "up" else self.children
        seen: set[str] = set()
        queue = deque([start])
        hops = 0
        while queue and hops < MAX_DEPTH:
            for _ in range(len(queue)):
                current = queue.popleft()
                for neighbour in step.get(current, ()):
                    if neighbour in seen or neighbour == start:
                        continue
                    seen.add(neighbour)
                    queue.append(neighbour)
            hops += 1
        return seen


def load_column_adjacency(
    session: Session, customer_id: str, tenant_id: str
) -> ColumnAdjacency:
    stmt = select(ColumnEdge).where(
        ColumnEdge.customer_id == customer_id, ColumnEdge.tenant_id == tenant_id
    )
    return ColumnAdjacency(list(session.scalars(stmt)))


def trace_column(adjacency: ColumnAdjacency, column_id: str) -> tuple[set[str], set[str]]:
    """Every column that feeds this one, and every column it feeds."""
    return adjacency.walk(column_id, "up"), adjacency.walk(column_id, "down")


def incoming_transform(adjacency: ColumnAdjacency, column_id: str) -> str:
    """
    The transform that produced this column, for the card label.

    A column fed by several upstream columns has several edges carrying the same
    expression (`LINEAMOUNT - LINEDISC` on both operands); any one of them is
    the right label, so the first is taken rather than joining duplicates.
    """
    for parent in adjacency.parents.get(column_id, ()):
        edge = adjacency.transform.get((parent, column_id))
        if edge and edge.transform_expr:
            return edge.transform_expr
    return ""


def load_nodes(
    session: Session, customer_id: str, tenant_id: str, node_ids: set[str]
) -> dict[str, DataNode]:
    if not node_ids:
        return {}
    out = {}
    for part in repository.chunks(node_ids):
        stmt = select(DataNode).where(
            DataNode.customer_id == customer_id,
            DataNode.tenant_id == tenant_id,
            DataNode.id.in_(part),
        )
        out.update({n.id: n for n in session.scalars(stmt)})
    return out


def load_columns(
    session: Session, customer_id: str, tenant_id: str, column_ids: set[str]
) -> dict[str, ColumnNode]:
    if not column_ids:
        return {}
    out = {}
    for part in repository.chunks(column_ids):
        stmt = select(ColumnNode).where(
            ColumnNode.customer_id == customer_id,
            ColumnNode.tenant_id == tenant_id,
            ColumnNode.id.in_(part),
        )
        out.update({c.id: c for c in session.scalars(stmt)})
    return out
