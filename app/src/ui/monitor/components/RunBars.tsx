import type { Bar } from "../api";
import { STATUS_LABEL, duration, when } from "../lib";

/**
 * The last runs as bars, oldest left: height is duration, colour is outcome —
 * a slow run and a failed run both stand out at a glance.
 */
export function RunBars({
  bars,
  height = 56,
  width = 9,
  selected,
  onPick,
}: {
  bars: Bar[];
  height?: number;
  width?: number;
  selected?: string | null;
  onPick?: (runId: string) => void;
}) {
  if (!bars.length) return <span className="muted-note">No runs</span>;
  // A running run's bar is its time so far.
  const secs = (b: Bar) =>
    b.durationSec ?? (b.status === "running" && b.startedAt ? (Date.now() - new Date(b.startedAt).getTime()) / 1000 : 0);
  // Finished runs set the scale; a run still going is clipped at the top.
  const max = Math.max(...bars.map((b) => b.durationSec ?? 0), 1);
  return (
    <div className="mon-bars" style={{ height }}>
      {bars.map((b) => {
        const h = Math.min(height - 4, Math.max(4, Math.round((secs(b) / max) * (height - 4))));
        const label = `${when(b.startedAt)} · ${STATUS_LABEL[b.status]} · ${duration(b.durationSec)}`;
        return (
          <button
            key={b.runId}
            type="button"
            className={`mon-bar mon-${b.status}${selected === b.runId ? " selected" : ""}`}
            style={{ height: h, width }}
            title={label}
            aria-label={label}
            onClick={onPick ? (e) => { e.stopPropagation(); onPick(b.runId); } : undefined}
            tabIndex={onPick ? 0 : -1}
          />
        );
      })}
    </div>
  );
}
