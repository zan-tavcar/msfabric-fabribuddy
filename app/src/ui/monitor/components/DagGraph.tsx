import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Maximize2, Minus, Plus } from "lucide-react";
import type { RunStatus } from "../api";
import { layoutDag } from "../lib";

export interface GraphNode {
  id: string;
  title: string;
  /** Second line: the item type, or the activity type. */
  subtitle?: string;
  status?: RunStatus | null;
  /** Bottom-right: a duration, "in session"… */
  note?: ReactNode;
  focus?: boolean;
  /** Drawn faded and dashed: not a run of its own (a %run include, a never-run item). */
  ghost?: boolean;
}

export interface GraphEdge {
  from: string;
  to: string;
  label?: string;
  /** Dashed: containment (a pipeline's own activities), not an ordering. */
  dashed?: boolean;
}

const W = 232;
const H = 74;
const GAP_X = 72;
const GAP_Y = 22;
const PAD = 28;

/**
 * Tasks as boxes, dependencies as curves, left to right — the execution chain
 * of an item or the task graph of one run. Status colours the border.
 */
export function DagGraph({
  nodes,
  edges,
  selected,
  onSelect,
  minHeight = 260,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  selected?: string | null;
  onSelect?: (id: string) => void;
  minHeight?: number;
}) {
  const [zoom, setZoom] = useState(1);
  const scrollRef = useRef<HTMLDivElement>(null);
  const pos = useMemo(
    () => layoutDag(nodes.map((n) => n.id), edges),
    [nodes, edges],
  );
  const cols = Math.max(0, ...[...pos.values()].map((p) => p.col)) + 1;
  const rows = Math.max(0, ...[...pos.values()].map((p) => p.row)) + 1;
  const width = PAD * 2 + cols * W + (cols - 1) * GAP_X;
  const height = Math.max(minHeight, PAD * 2 + rows * H + (rows - 1) * GAP_Y);
  // Open fitted to the panel's width, between 80% (still readable — wider
  // graphs scroll) and 100%; the reset button fits again.
  const fit = () => {
    const el = scrollRef.current;
    if (!el || !width) return;
    setZoom(Math.max(0.8, Math.min(1, (el.clientWidth - 4) / width)));
  };
  useEffect(fit, [width]);
  const xy = (id: string) => {
    const p = pos.get(id)!;
    return { x: PAD + p.col * (W + GAP_X), y: PAD + p.row * (H + GAP_Y) };
  };

  return (
    <div className="mon-graph">
      <div className="mon-graph-tools">
        <button type="button" className="btn btn-icon btn-sm" aria-label="Zoom in" onClick={() => setZoom((z) => Math.min(1.6, z + 0.15))}>
          <Plus size={14} />
        </button>
        <button type="button" className="btn btn-icon btn-sm" aria-label="Zoom out" onClick={() => setZoom((z) => Math.max(0.45, z - 0.15))}>
          <Minus size={14} />
        </button>
        <button type="button" className="btn btn-icon btn-sm" aria-label="Fit to width" onClick={fit}>
          <Maximize2 size={13} />
        </button>
      </div>
      <div ref={scrollRef} className="mon-graph-scroll" style={{ minHeight: Math.min(height * zoom, 640) }}>
        <div style={{ width: width * zoom, height: height * zoom, position: "relative" }}>
          <div style={{ width, height, transform: `scale(${zoom})`, transformOrigin: "0 0", position: "absolute" }}>
            <svg width={width} height={height} className="mon-graph-edges" aria-hidden="true">
              <defs>
                <marker id="mon-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--line-strong)" />
                </marker>
              </defs>
              {edges.map((e, i) => {
                if (!pos.has(e.from) || !pos.has(e.to)) return null;
                const a = xy(e.from);
                const b = xy(e.to);
                const x1 = a.x + W;
                const y1 = a.y + H / 2;
                const x2 = b.x - 2;
                const y2 = b.y + H / 2;
                const mid = (x1 + x2) / 2;
                const hot = selected && (e.from === selected || e.to === selected);
                return (
                  <g key={i}>
                    <path
                      d={`M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`}
                      fill="none"
                      stroke={hot ? "var(--blue)" : "var(--line-strong)"}
                      strokeWidth={hot ? 2 : 1.4}
                      strokeDasharray={e.dashed ? "5 4" : undefined}
                      markerEnd="url(#mon-arrow)"
                    />
                    {e.label && (
                      <text x={mid} y={(y1 + y2) / 2 - 5} textAnchor="middle" className="mon-edge-label">
                        {e.label}
                      </text>
                    )}
                  </g>
                );
              })}
            </svg>
            {nodes.map((n) => {
              const { x, y } = xy(n.id);
              const classes = [
                "mon-node",
                `mon-node-${n.status ?? "none"}`,
                n.focus ? "focus" : "",
                n.ghost ? "ghost" : "",
                selected === n.id ? "selected" : "",
              ]
                .filter(Boolean)
                .join(" ");
              return (
                <button
                  key={n.id}
                  type="button"
                  className={classes}
                  style={{ left: x, top: y, width: W, height: H }}
                  onClick={() => onSelect?.(n.id)}
                  title={n.title}
                >
                  <span className="mon-node-title">{n.title}</span>
                  <span className="mon-node-sub">{n.subtitle}</span>
                  <span className="mon-node-foot">
                    {n.status && <span className={`mon-node-state mon-${n.status}`}>{n.status}</span>}
                    <span className="mon-node-note">{n.note}</span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
