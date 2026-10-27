import { useLayoutEffect, useRef, useState } from "react";
import type { FlowLane, FlowLink, Layer, Overview } from "../api/types";

/** A lane's name on its block, plural. */
const LANE_TITLE: Record<Layer, string> = {
  source: "External sources",
  mirrored: "Mirrored tables",
  lakehouse: "Lakehouse tables",
  warehouse: "Warehouse tables",
  semantic: "Semantic models",
  report: "Reports",
};

/** What one thing in a lane is called, singular and plural, for the link sentences. */
const UNIT: Record<Layer, [string, string]> = {
  source: ["external source", "external sources"],
  mirrored: ["mirrored table", "mirrored tables"],
  lakehouse: ["lakehouse table", "lakehouse tables"],
  warehouse: ["warehouse table", "warehouse tables"],
  semantic: ["semantic model", "semantic models"],
  report: ["report", "reports"],
};

const CONTAINER: Partial<Record<Layer, [string, string]>> = {
  lakehouse: ["lakehouse", "lakehouses"],
  warehouse: ["warehouse", "warehouses"],
  mirrored: ["mirrored database", "mirrored databases"],
};

/** "4 lakehouse tables are fed by external sources." */
export function linkSentence(link: FlowLink): string {
  const [one, many] = UNIT[link.to];
  const from = UNIT[link.from][1];
  const subject = `${link.count} ${link.count === 1 ? one : many}`;
  const verb = link.count === 1 ? "is" : "are";
  return link.from === link.to
    ? `${subject} ${verb} built from other ${from}`
    : `${subject} ${verb} fed by ${from}`;
}

function laneSub(lane: FlowLane): string {
  const container = CONTAINER[lane.layer];
  if (container && lane.containers > 0) {
    return `in ${lane.containers} ${lane.containers === 1 ? container[0] : container[1]}`;
  }
  if (lane.layer === "source") return "outside Fabric";
  return "";
}

type Box = { left: number; right: number; top: number; bottom: number; mid: number };

/**
 * The estate as one line, source to report: a block per lane and a band per
 * link, as thick as what it feeds. A block opens the lineage graph with that
 * lane highlighted, so the summary is also the way in. Links inside a lane
 * (bronze to silver) are the graph's business, not this line's.
 */
export function FlowMap({
  flow,
  onOpenLane,
}: {
  flow: Overview["flow"];
  /** Opens the lineage graph with this lane highlighted. */
  onOpenLane: (layer: Layer) => void;
}) {
  const lanes = flow.lanes.filter((lane) => lane.count > 0);
  const order = lanes.map((lane) => lane.layer);
  const links = flow.links.filter(
    (link) => link.from !== link.to && order.includes(link.from) && order.includes(link.to),
  );
  const wrap = useRef<HTMLDivElement>(null);
  const blocks = useRef<Partial<Record<Layer, HTMLButtonElement | null>>>({});
  const [boxes, setBoxes] = useState<Partial<Record<Layer, Box>>>({});

  const orderKey = order.join(",");
  useLayoutEffect(() => {
    const root = wrap.current;
    if (!root) return;
    const measure = () => {
      const origin = root.getBoundingClientRect();
      const next: Partial<Record<Layer, Box>> = {};
      for (const layer of orderKey.split(",") as Layer[]) {
        const rect = blocks.current[layer]?.getBoundingClientRect();
        if (!rect) continue;
        next[layer] = {
          left: rect.left - origin.left,
          right: rect.right - origin.left,
          top: rect.top - origin.top,
          bottom: rect.bottom - origin.top,
          mid: rect.left - origin.left + rect.width / 2,
        };
      }
      setBoxes(next);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    return () => observer.disconnect();
  }, [orderKey]);

  if (lanes.length === 0) return null;

  const max = Math.max(1, ...links.map((link) => link.count));
  const width = (count: number) => 2 + (count / max) * 8;
  // Links that jump over a lane run underneath the blocks, one tier each.
  const skips = links.filter(
    (link) => Math.abs(order.indexOf(link.to) - order.indexOf(link.from)) > 1,
  );

  const drawn = links.flatMap((link) => {
    const a = boxes[link.from];
    const b = boxes[link.to];
    if (!a || !b) return [];
    const gap = order.indexOf(link.to) - order.indexOf(link.from);
    const y = (a.top + a.bottom) / 2;
    let d: string;
    let label: { x: number; y: number };
    if (gap === 1) {
      d = `M ${a.right} ${y} L ${b.left} ${y}`;
      label = { x: (a.right + b.left) / 2, y };
    } else {
      const depth = 34 + skips.indexOf(link) * 22;
      d = `M ${a.mid} ${a.bottom} C ${a.mid} ${a.bottom + depth}, ${b.mid} ${b.bottom + depth}, ${b.mid} ${b.bottom}`;
      label = { x: (a.mid + b.mid) / 2, y: a.bottom + depth * 0.75 };
    }
    return [{ link, d, label }];
  });

  return (
    <div
      className="flow-map"
      ref={wrap}
      style={{
        paddingTop: 8,
        paddingBottom: skips.length ? 30 + skips.length * 22 : 8,
      }}
    >
      <svg className="flow-links" aria-hidden="true">
        <defs>
          {order.map((layer) => (
            <marker
              key={layer}
              id={`flow-arrow-${layer}`}
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerUnits="userSpaceOnUse"
              markerWidth="9"
              markerHeight="9"
              orient="auto-start-reverse"
            >
              <path d="M0 0 L10 5 L0 10 z" className={`flow-arrow-${layer}`} />
            </marker>
          ))}
        </defs>
        {drawn.map(({ link, d }) => (
          <path
            key={`${link.from}-${link.to}`}
            d={d}
            className={`flow-link flow-from-${link.from}`}
            strokeWidth={width(link.count)}
            markerEnd={`url(#flow-arrow-${link.from})`}
          />
        ))}
      </svg>

      <div className="flow-lanes">
        {lanes.map((lane) => (
          <button
            key={lane.layer}
            type="button"
            ref={(el) => {
              blocks.current[lane.layer] = el;
            }}
            className={`flow-lane flow-lane-${lane.layer}`}
            onClick={() => onOpenLane(lane.layer)}
            title={`Show the ${LANE_TITLE[lane.layer].toLowerCase()} in the lineage graph`}
          >
            <span className="flow-lane-title">
              <span className={`layer-dot layer-${lane.layer}`} aria-hidden="true" />
              {LANE_TITLE[lane.layer]}
            </span>
            <span className="flow-lane-count tnum">{lane.count}</span>
            <span className="flow-lane-sub">{laneSub(lane) || " "}</span>
          </button>
        ))}
      </div>

      {drawn.map(({ link, label }) => (
        <span
          key={`${link.from}-${link.to}`}
          className="flow-count tnum"
          style={{ left: label.x, top: label.y }}
          title={linkSentence(link)}
        >
          {link.count}
        </span>
      ))}
    </div>
  );
}
