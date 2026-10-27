import { useEffect, useState } from "react";
import { CheckCircle2, Loader2, TriangleAlert } from "lucide-react";
import type { ScanPass, ScanProgress } from "../api/types";

const PRODUCT_NAMES = { lineage: "Data Catalog", audience: "Report Audience" } as const;

/** 75 -> "1m 15s"; 3725 -> "1h 2m". */
export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** Seconds since an ISO time, ticking once a second while shown. */
function useElapsed(since: string | null | undefined): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!since) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [since]);
  if (!since) return null;
  // The API stores naive UTC; without a zone the browser would read local time.
  const started = Date.parse(/[zZ]|[+-]\d\d:\d\d$/.test(since) ? since : `${since}Z`);
  return Number.isNaN(started) ? null : (now - started) / 1000;
}

/**
 * The strip under the top bar while a scan runs: what is being read right now,
 * how far along the pass is, and for how long. Shown to every member, whoever
 * started the scan (a person, the schedule, another admin), because anyone
 * looking at the data should know it is about to change.
 *
 * When the pass ends it says so for a few seconds (`finished`), so a scan that
 * completes while someone watches does not simply vanish.
 */
export function ScanProgressStrip({
  progress,
  finished,
}: {
  progress: ScanProgress | null;
  finished: ScanPass | null;
}) {
  const live = progress?.running ? progress.pass : null;
  const elapsed = useElapsed(live?.startedAt);

  if (live) {
    const counted = live.total ? `${live.done ?? 0} of ${live.total}` : null;
    // Both products, each with its own state, on the one line.
    const products =
      live.products && live.products.length
        ? live.products
        : live.product
          ? [{ product: live.product, steps: live.stepsTotal, done: live.stepsDone, state: "running" as const }]
          : [];
    return (
      <div className="scan-strip" role="status" aria-live="polite">
        <div className="scan-strip-row">
          <Loader2 className="spin scan-strip-icon" size={15} aria-hidden="true" />
          <span className="scan-strip-title">Scanning</span>
          {products.map((p) => (
            <span
              key={p.product}
              className={`scan-strip-product ${p.state}`}
              title={`${PRODUCT_NAMES[p.product]}: ${
                p.state === "done" ? "done" : p.state === "waiting" ? "waiting" : `step ${p.done + 1} of ${p.steps}`
              }`}
            >
              {p.state === "done" ? (
                <CheckCircle2 size={12} aria-hidden="true" />
              ) : (
                <i className="scan-strip-dot" aria-hidden="true" />
              )}
              {PRODUCT_NAMES[p.product]}
              <span className="scan-strip-steps tnum">
                {p.state === "done" ? "done" : p.state === "waiting" ? "next" : `${p.done + 1}/${p.steps}`}
              </span>
            </span>
          ))}
          <span className="scan-strip-label">
            {live.label}
            {counted && <span className="scan-strip-count"> · {counted}</span>}
          </span>
          {live.detail && (
            <span className="scan-strip-detail" title={live.detail}>
              {live.detail}
            </span>
          )}
          <span className="scan-strip-meta tnum">
            {elapsed != null ? duration(elapsed) : ""}
          </span>
          <span className="scan-strip-percent tnum">{live.percent}%</span>
        </div>
        <div
          className="scan-strip-bar"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={live.percent}
          aria-label="Scan progress, both products"
        >
          <span style={{ width: `${Math.max(2, live.percent)}%` }} />
        </div>
      </div>
    );
  }

  if (finished) {
    const ok = finished.status === "ok";
    return (
      <div className={`scan-strip scan-strip-done ${ok ? "ok" : "warn"}`} role="status">
        <div className="scan-strip-row">
          {ok ? (
            <CheckCircle2 size={15} className="scan-strip-icon" aria-hidden="true" />
          ) : (
            <TriangleAlert size={15} className="scan-strip-icon" aria-hidden="true" />
          )}
          <span className="scan-strip-title">
            {ok
              ? "Scan finished"
              : finished.status === "partial"
                ? "Scan finished with gaps"
                : "Scan failed"}
          </span>
          <span className="scan-strip-label">
            {duration(finished.durationSeconds)} ·{" "}
            {finished.products.map((p) => PRODUCT_NAMES[p]).join(" and ")}
            {finished.stepsFailed > 0 &&
              ` · ${finished.stepsFailed} step${finished.stepsFailed === 1 ? "" : "s"} failed`}
          </span>
        </div>
      </div>
    );
  }
  return null;
}
