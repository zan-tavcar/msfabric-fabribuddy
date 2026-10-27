import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import { ArrowRight, Folder, RotateCcw, X } from "lucide-react";
import type { GraphPayload, Layer } from "../api/types";
import {
  NODE_HEIGHT,
  NODE_WIDTH,
  type Placed,
  edgePath,
  layout,
  tracePath,
} from "../lib/graphLayout";
import { LAYER_LABEL, LAYER_ORDER } from "../lib/layers";

type Point = { x: number; y: number };
type Positions = Record<string, Point>;

/** Movement below this is a click, not a drag. */
const DRAG_THRESHOLD = 4;
/** Shift+Arrow nudges a focused card this far. */
const NUDGE = 12;
const CANVAS_MARGIN = 60;
const STORAGE_PREFIX = "fabribuddy:graph-layout:";

function loadPositions(key: string | undefined): Positions {
  if (!key) return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_PREFIX + key);
    return raw ? (JSON.parse(raw) as Positions) : {};
  } catch {
    return {};
  }
}

function savePositions(key: string | undefined, positions: Positions): void {
  if (!key) return;
  try {
    if (Object.keys(positions).length === 0) {
      window.localStorage.removeItem(STORAGE_PREFIX + key);
    } else {
      window.localStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(positions));
    }
  } catch {
    // storage unavailable (private mode): the arrangement lasts this session
  }
}

type Drag =
  | {
      kind: "node";
      id: string;
      pointerId: number;
      startX: number;
      startY: number;
      origin: Point;
      moved: boolean;
    }
  | {
      kind: "pan";
      pointerId: number;
      startX: number;
      startY: number;
      scrollLeft: number;
      scrollTop: number;
      moved: boolean;
    };

/**
 * The DAG, in lanes by layer, with cards you can drag.
 *
 * SVG for the edges beneath absolutely-positioned buttons for the nodes: the
 * nodes have to be real buttons (keyboard-reachable, clickable) and edges have
 * to be curves, so neither a pure-SVG nor a pure-DOM rendering does both.
 *
 * Clicking a card traces its whole lineage — everything upstream and
 * downstream, lit in the accent colour, the rest dimmed — and offers "View table details"
 * above it; only that opens the table. Clicking empty canvas (or Escape)
 * clears it. Dragging a card moves it and re-routes its edges live; dragging
 * empty canvas pans.
 * Moved cards are remembered per viewer under `layoutKey` (the selector and
 * filters that produced this graph), because a position only means something
 * in the graph it was arranged in. Shift+Arrow nudges a focused card.
 */
export function LineageGraph({
  payload,
  onPick,
  dimOffPath = false,
  highlightLayer,
  focusId,
  layoutKey,
}: {
  payload: GraphPayload;
  onPick: (nodeId: string) => void;
  /** Highlight the focused node's path and dim everything else. */
  dimOffPath?: boolean;
  /** Highlight one lane's cards (from the overview's flow) and dim the rest. */
  highlightLayer?: Layer | null;
  /** The table whose lineage was asked for (a catalog row, a table page). */
  focusId?: string | null;
  /** Remember dragged positions under this key; omitted = this session only. */
  layoutKey?: string;
}) {
  const computed = useMemo(
    () => layout(payload.nodes, payload.edges, payload.lanes),
    [payload],
  );
  const [moved, setMoved] = useState<Positions>(() => loadPositions(layoutKey));
  const [dragging, setDragging] = useState<string | null>(null);
  // The card whose lineage is traced, and the path itself.
  const [picked, setPicked] = useState<string | null>(null);
  const traced = useMemo(
    () => (picked && computed.byId.has(picked) ? tracePath(payload.edges, picked) : null),
    [picked, payload, computed],
  );
  useEffect(() => setPicked(null), [payload]);
  const drag = useRef<Drag | null>(null);
  // A drag ends in a click on the card it started on; that click is not "open".
  const suppressClick = useRef(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const movedRef = useRef(moved);
  movedRef.current = moved;

  useEffect(() => {
    setMoved(loadPositions(layoutKey));
  }, [layoutKey]);

  // A highlighted lane is scrolled into view: on a wide estate it may start
  // far to the right of sources and lakehouses.
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap || !(highlightLayer || focusId)) return;
    const xs = computed.placed
      .filter((p) => (focusId ? p.node.id === focusId : p.node.layer === highlightLayer))
      .map((p) => p.x);
    if (xs.length) wrap.scrollLeft = Math.max(0, Math.min(...xs) - 240);
  }, [highlightLayer, focusId, computed]);

  // Positions of nodes no longer in the graph are dropped, not kept forever.
  const liveMoved = useMemo(() => {
    const out: Positions = {};
    for (const [id, p] of Object.entries(moved)) if (computed.byId.has(id)) out[id] = p;
    return out;
  }, [moved, computed]);

  const placedById = useMemo(() => {
    const map = new Map<string, Placed>();
    computed.placed.forEach((p) => {
      const override = liveMoved[p.node.id];
      map.set(p.node.id, override ? { ...p, ...override } : p);
    });
    return map;
  }, [computed, liveMoved]);

  const bounds = useMemo(() => {
    let width = computed.width;
    let height = computed.height;
    placedById.forEach((p) => {
      width = Math.max(width, p.x + NODE_WIDTH + CANVAS_MARGIN);
      height = Math.max(height, p.y + NODE_HEIGHT * 2 + CANVAS_MARGIN);
    });
    return { width, height };
  }, [computed, placedById]);

  const commit = useCallback(
    (next: Positions) => {
      setMoved(next);
      savePositions(layoutKey, next);
    },
    [layoutKey],
  );

  const moveNode = (id: string, point: Point) =>
    setMoved((current) => ({
      ...current,
      [id]: { x: Math.max(0, Math.round(point.x)), y: Math.max(0, Math.round(point.y)) },
    }));

  const onNodePointerDown = (event: ReactPointerEvent<HTMLButtonElement>, id: string) => {
    if (event.button !== 0) return;
    const at = placedById.get(id);
    if (!at) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = {
      kind: "node",
      id,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      origin: { x: at.x, y: at.y },
      moved: false,
    };
  };

  const onPointerMove = (event: ReactPointerEvent) => {
    const d = drag.current;
    if (!d || d.pointerId !== event.pointerId) return;
    const dx = event.clientX - d.startX;
    const dy = event.clientY - d.startY;
    if (d.kind === "pan") {
      if (Math.hypot(dx, dy) >= DRAG_THRESHOLD) d.moved = true;
      const wrap = wrapRef.current;
      if (wrap) {
        wrap.scrollLeft = d.scrollLeft - dx;
        wrap.scrollTop = d.scrollTop - dy;
      }
      return;
    }
    if (!d.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    if (!d.moved) {
      d.moved = true;
      setDragging(d.id);
    }
    moveNode(d.id, { x: d.origin.x + dx, y: d.origin.y + dy });
  };

  const onPointerUp = (event: ReactPointerEvent) => {
    const d = drag.current;
    if (!d || d.pointerId !== event.pointerId) return;
    drag.current = null;
    if (d.kind === "pan") {
      wrapRef.current?.classList.remove("panning");
      // A click on empty canvas (not a pan) clears the traced path.
      if (!d.moved) setPicked(null);
      return;
    }
    setDragging(null);
    if (d.moved) {
      suppressClick.current = true;
      savePositions(layoutKey, movedRef.current);
    }
  };

  const onNodeClick = (id: string) => {
    if (suppressClick.current) {
      suppressClick.current = false;
      return;
    }
    setPicked((current) => (current === id ? null : id));
  };

  const onCanvasPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    // Only the empty canvas pans; a card handles its own press.
    if (event.button !== 0 || event.target !== event.currentTarget) return;
    const wrap = wrapRef.current;
    if (!wrap) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    wrap.classList.add("panning");
    drag.current = {
      kind: "pan",
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      scrollLeft: wrap.scrollLeft,
      scrollTop: wrap.scrollTop,
      moved: false,
    };
  };

  const onNodeKeyDown = (event: KeyboardEvent<HTMLButtonElement>, id: string) => {
    if (!event.shiftKey) return;
    const step = {
      ArrowLeft: [-NUDGE, 0],
      ArrowRight: [NUDGE, 0],
      ArrowUp: [0, -NUDGE],
      ArrowDown: [0, NUDGE],
    }[event.key];
    const at = placedById.get(id);
    if (!step || !at) return;
    event.preventDefault();
    const next = {
      ...liveMoved,
      [id]: { x: Math.max(0, at.x + step[0]), y: Math.max(0, at.y + step[1]) },
    };
    commit(next);
  };

  if (payload.nodes.length === 0) {
    return (
      <div className="graph-wrap">
        <p className="muted-note" style={{ padding: 16 }}>
          Nothing to draw. Run a scan, or widen the selector.
        </p>
      </div>
    );
  }

  const movedCount = Object.keys(liveMoved).length;
  const bottom = bounds.height - NODE_HEIGHT;

  return (
    <div className="graph-frame">
      <div className="graph-tools">
        <span className="muted-note">
          Click a table to trace its lineage · drag to rearrange · drag the background to pan
        </span>
        {movedCount > 0 && (
          <button type="button" className="btn btn-sm" onClick={() => commit({})}>
            <RotateCcw size={13} aria-hidden="true" />
            Reset layout ({movedCount} moved)
          </button>
        )}
      </div>
      <div
        className="graph-wrap"
        ref={wrapRef}
        onKeyDown={(e) => {
          if (e.key === "Escape") setPicked(null);
        }}
      >
        <div
          className="graph-canvas"
          style={{ width: bounds.width, height: bounds.height }}
          onPointerDown={onCanvasPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
        >
          {computed.lanes.map((lane) => (
            <div
              key={lane.label}
              className="lane-label"
              style={{ left: lane.x, width: lane.width }}
            >
              {lane.label}
            </div>
          ))}

          <svg
            width={bounds.width}
            height={bounds.height}
            style={{ position: "absolute", left: 0, top: 0, pointerEvents: "none" }}
            aria-hidden="true"
          >
            {payload.edges.map((edge, index) => {
              const from = placedById.get(edge.from);
              const to = placedById.get(edge.to);
              if (!from || !to) return null;
              const held = dragging !== null && (edge.from === dragging || edge.to === dragging);
              const onTrace = traced?.edges.has(index) ?? false;
              const lit = held || onTrace || (!traced && dimOffPath && edge.onPath);
              return (
                <path
                  key={`${edge.from}->${edge.to}-${index}`}
                  d={edgePath(from, to, bottom)}
                  fill="none"
                  stroke={lit ? "var(--accent)" : "#b8c0cc"}
                  strokeWidth={lit ? 2.5 : 1.5}
                  strokeDasharray={edge.isDynamic ? "5 4" : undefined}
                  opacity={
                    dragging !== null
                      ? held
                        ? 1
                        : 0.35
                      : traced
                        ? onTrace
                          ? 1
                          : 0.18
                        : highlightLayer
                          ? from.node.layer === highlightLayer || to.node.layer === highlightLayer
                            ? 1
                            : 0.25
                          : dimOffPath && !edge.onPath
                            ? 0.35
                            : 1
                  }
                />
              );
            })}
          </svg>

          {computed.placed.map(({ node }) => {
            const at = placedById.get(node.id)!;
            const lit = !traced && Boolean(highlightLayer) && node.layer === highlightLayer;
            const dim = traced
              ? !traced.nodes.has(node.id)
              : highlightLayer
                ? !lit
                : dimOffPath && !node.onPath;
            const classes = [
              "graph-node",
              (traced ? picked === node.id : node.isFocus || node.id === focusId) ? "focus" : "",
              traced && traced.nodes.has(node.id) && picked !== node.id ? "traced" : "",
              dim ? "dim" : "",
              lit ? "lit" : "",
              dragging === node.id ? "dragging" : "",
              liveMoved[node.id] ? "moved" : "",
            ]
              .filter(Boolean)
              .join(" ");
            return (
              <button
                key={node.id}
                type="button"
                className={classes}
                style={{ left: at.x, top: at.y, width: NODE_WIDTH, height: NODE_HEIGHT }}
                onPointerDown={(e) => onNodePointerDown(e, node.id)}
                onClick={() => onNodeClick(node.id)}
                onKeyDown={(e) => onNodeKeyDown(e, node.id)}
                title={`${node.path}\nClick to trace its lineage · drag to move`}
                aria-roledescription="draggable table"
                aria-pressed={picked === node.id}
              >
                <span className="name">
                  <span className={`layer-dot layer-${node.layer}`} aria-hidden="true" />
                  {node.shortName}
                </span>
                <span className="sub">{node.sub}</span>
                {node.workspace && (
                  <span className="ws" title={`Workspace: ${node.workspace}`}>
                    <Folder size={10} strokeWidth={2} aria-hidden="true" />
                    {node.workspace}
                  </span>
                )}
              </button>
            );
          })}

          {traced && picked && placedById.get(picked) && (
            <TracePopover
              at={placedById.get(picked)!}
              name={placedById.get(picked)!.node.name}
              upstream={traced.upstream}
              downstream={traced.downstream}
              onOpen={() => onPick(picked)}
              onClose={() => setPicked(null)}
            />
          )}
        </div>
      </div>
    </div>
  );
}

/** Above the picked card: what the trace shows, and the way into the table. */
function TracePopover({
  at,
  name,
  upstream,
  downstream,
  onOpen,
  onClose,
}: {
  at: Placed;
  name: string;
  upstream: number;
  downstream: number;
  onOpen: () => void;
  onClose: () => void;
}) {
  const above = at.y >= 50;
  return (
    <div
      className={`trace-pop ${above ? "above" : "below"}`}
      style={{
        left: at.x + NODE_WIDTH / 2,
        top: above ? at.y - 10 : at.y + NODE_HEIGHT + 10,
      }}
      role="dialog"
      aria-label={`Lineage of ${name}`}
      onPointerDown={(e) => e.stopPropagation()}
    >
      <span className="trace-pop-counts tnum" title={`${upstream} upstream, ${downstream} downstream`}>
        ↑{upstream} ↓{downstream}
      </span>
      <button type="button" className="trace-pop-open" onClick={onOpen} autoFocus>
        View table details <ArrowRight size={13} aria-hidden="true" />
      </button>
      <button type="button" className="trace-pop-close" aria-label="Clear the trace" onClick={onClose}>
        <X size={12} aria-hidden="true" />
      </button>
    </div>
  );
}

export function GraphLegend() {
  return (
    <div className="legend">
      {LAYER_ORDER.map((layer) => (
        <span key={layer}>
          <span className={`layer-dot layer-${layer}`} aria-hidden="true" />
          {LAYER_LABEL[layer]}
        </span>
      ))}
    </div>
  );
}
