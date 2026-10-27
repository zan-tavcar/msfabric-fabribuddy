import type { GraphEdge, GraphNode, Lane } from "../api/types";
import { LAYER_ORDER } from "./layers";

/**
 * Lane-constrained layered layout.
 *
 * Nodes stay in their storage lane — that constraint is the point of the view —
 * but a lane is split into sub-columns by its *internal* depth: a lakehouse
 * table built from another lakehouse table sits one sub-column to the right of
 * it. Without that, an edge between two tables of the same lane had to run
 * back through the cards between them. Every edge then points left to right.
 *
 * Within a column, nodes are ordered by the barycenter heuristic, swept down
 * and up a few times: each node moves toward the average position of its
 * neighbours in the adjacent columns, which removes most crossings.
 */

export const NODE_WIDTH = 190;
export const NODE_HEIGHT = 64;
export const LANE_GAP = 26;
/** Gap between sub-columns inside one lane — wide enough for a readable curve. */
export const COLUMN_GAP = 56;
export const ROW_GAP = 22;
export const TOP_PADDING = 28;
const SWEEPS = 4;

export interface Placed {
  node: GraphNode;
  x: number;
  y: number;
}

export interface Layout {
  placed: Placed[];
  byId: Map<string, Placed>;
  lanes: { label: string; x: number; width: number }[];
  width: number;
  height: number;
}

export function layout(nodes: GraphNode[], edges: GraphEdge[], lanes: Lane[]): Layout {
  const ids = new Set(nodes.map((n) => n.id));
  const live = edges.filter((e) => ids.has(e.from) && ids.has(e.to) && e.from !== e.to);
  const parents = new Map<string, string[]>();
  const children = new Map<string, string[]>();
  live.forEach((e) => {
    parents.set(e.to, [...(parents.get(e.to) ?? []), e.from]);
    children.set(e.from, [...(children.get(e.from) ?? []), e.to]);
  });
  const layerOf = new Map(nodes.map((n) => [n.id, n.layer]));

  // Depth inside the node's own lane: 0 unless a same-lane parent feeds it.
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  const laneDepth = (id: string): number => {
    const known = depth.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) return 0; // a cycle — stop rather than recurse forever
    visiting.add(id);
    const sameLane = (parents.get(id) ?? []).filter((p) => layerOf.get(p) === layerOf.get(id));
    const value = sameLane.length ? 1 + Math.max(...sameLane.map(laneDepth)) : 0;
    visiting.delete(id);
    depth.set(id, value);
    return value;
  };
  nodes.forEach((n) => laneDepth(n.id));

  // Columns: lanes in order, each split into its sub-columns.
  const columns: GraphNode[][] = [];
  const laneSpans: { label: string; first: number; count: number }[] = [];
  LAYER_ORDER.forEach((layer) => {
    const members = nodes.filter((n) => n.layer === layer);
    if (members.length === 0) return;
    const count = Math.max(...members.map((n) => depth.get(n.id) ?? 0)) + 1;
    const first = columns.length;
    for (let i = 0; i < count; i += 1) {
      columns.push(members.filter((n) => (depth.get(n.id) ?? 0) === i));
    }
    laneSpans.push({
      label: lanes.find((l) => l.layer === layer)?.label ?? layer,
      first,
      count,
    });
  });

  // Barycenter ordering, swept forward (by parents) and backward (by children).
  const position = new Map<string, number>();
  columns.forEach((column) => {
    column.sort((a, b) => a.name.localeCompare(b.name));
    column.forEach((n, i) => position.set(n.id, i));
  });
  const reorder = (column: GraphNode[], neighbours: Map<string, string[]>) => {
    const score = (n: GraphNode) => {
      const known = (neighbours.get(n.id) ?? [])
        .map((m) => position.get(m))
        .filter((v): v is number => v !== undefined);
      return known.length ? known.reduce((a, b) => a + b, 0) / known.length : position.get(n.id)!;
    };
    const scored = column.map((n) => ({ n, s: score(n) }));
    scored.sort((a, b) => a.s - b.s || a.n.name.localeCompare(b.n.name));
    scored.forEach(({ n }, i) => {
      column[i] = n;
      position.set(n.id, i);
    });
  };
  for (let sweep = 0; sweep < SWEEPS; sweep += 1) {
    for (let c = 1; c < columns.length; c += 1) reorder(columns[c], parents);
    for (let c = columns.length - 2; c >= 0; c -= 1) reorder(columns[c], children);
  }

  // Coordinates. Sub-columns of one lane are COLUMN_GAP apart; lanes LANE_GAP.
  const xOf: number[] = [];
  let x = 0;
  laneSpans.forEach((span, laneIndex) => {
    if (laneIndex > 0) x += LANE_GAP;
    for (let i = 0; i < span.count; i += 1) {
      if (i > 0) x += COLUMN_GAP;
      xOf[span.first + i] = x;
      x += NODE_WIDTH;
    }
  });

  const placed: Placed[] = [];
  const byId = new Map<string, Placed>();
  columns.forEach((column, c) => {
    column.forEach((node, row) => {
      const entry = { node, x: xOf[c], y: TOP_PADDING + row * (NODE_HEIGHT + ROW_GAP) };
      placed.push(entry);
      byId.set(node.id, entry);
    });
  });

  const tallest = Math.max(1, ...columns.map((c) => c.length));
  const height = TOP_PADDING + tallest * (NODE_HEIGHT + ROW_GAP);
  return {
    placed,
    byId,
    lanes: laneSpans.map((span) => ({
      label: span.label,
      x: xOf[span.first],
      width: xOf[span.first + span.count - 1] + NODE_WIDTH - xOf[span.first],
    })),
    width: Math.max(NODE_WIDTH, x),
    height: height + NODE_HEIGHT, // room for edges routed underneath
  };
}

/**
 * A left-to-right bezier between two placed nodes.
 *
 * An edge that would run backwards (a cycle, or storage feeding an earlier
 * lane) is routed below the rows instead of through the cards between them.
 */
export function edgePath(from: Placed, to: Placed, bottom?: number): string {
  const x1 = from.x + NODE_WIDTH;
  const y1 = from.y + NODE_HEIGHT / 2;
  const x2 = to.x;
  const y2 = to.y + NODE_HEIGHT / 2;
  if (x2 > x1) {
    const mid = (x1 + x2) / 2;
    return `M${x1} ${y1} C${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`;
  }
  const below = (bottom ?? Math.max(from.y, to.y) + NODE_HEIGHT) + 14;
  return (
    `M${x1} ${y1} C${x1 + 30} ${y1}, ${x1 + 30} ${below}, ${x1} ${below} ` +
    `L${x2} ${below} C${x2 - 30} ${below}, ${x2 - 30} ${y2}, ${x2} ${y2}`
  );
}

export interface TracedPath {
  /** The picked node, everything upstream of it and everything downstream. */
  nodes: Set<string>;
  /** Indexes into the edge list of the edges along those paths. */
  edges: Set<number>;
  upstream: number;
  downstream: number;
}

/**
 * The whole lineage of one node: every ancestor and every descendant,
 * transitively, and the edges that connect them to it.
 *
 * An edge is on the path when both its ends are upstream of the node (so it
 * leads, eventually, into it) or both are downstream. An edge that skips over
 * the node — from an ancestor straight to a descendant — is lineage *around*
 * it, not through it, and stays unlit.
 */
export function tracePath(edges: GraphEdge[], id: string): TracedPath {
  const walk = (next: Map<string, string[]>) => {
    const seen = new Set<string>();
    const queue = [id];
    while (queue.length) {
      const current = queue.shift()!;
      for (const n of next.get(current) ?? []) {
        if (n !== id && !seen.has(n)) {
          seen.add(n);
          queue.push(n);
        }
      }
    }
    return seen;
  };
  const forward = new Map<string, string[]>();
  const backward = new Map<string, string[]>();
  edges.forEach((e) => {
    forward.set(e.from, [...(forward.get(e.from) ?? []), e.to]);
    backward.set(e.to, [...(backward.get(e.to) ?? []), e.from]);
  });
  const up = walk(backward);
  const down = walk(forward);
  const upside = new Set([...up, id]);
  const downside = new Set([...down, id]);
  const onPath = new Set<number>();
  edges.forEach((e, index) => {
    if ((upside.has(e.from) && upside.has(e.to)) || (downside.has(e.from) && downside.has(e.to))) {
      onPath.add(index);
    }
  });
  return {
    nodes: new Set([id, ...up, ...down]),
    edges: onPath,
    upstream: up.size,
    downstream: down.size,
  };
}
