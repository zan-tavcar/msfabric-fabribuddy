import { Ban, Check, CircleDashed, Clock, Loader2, SkipForward, X } from "lucide-react";
import type { RunStatus } from "../api";
import { STATUS_LABEL } from "../lib";

const ICON = {
  success: Check,
  failed: X,
  running: Loader2,
  queued: Clock,
  cancelled: Ban,
  skipped: SkipForward,
  unknown: CircleDashed,
} as const;

/** A run's state: green tick, red cross, spinner… always with its word. */
export function StatusBadge({
  status,
  label,
  compact,
}: {
  status: RunStatus;
  label?: string;
  compact?: boolean;
}) {
  const Icon = ICON[status];
  return (
    <span className={`mon-status mon-${status}${compact ? " compact" : ""}`}>
      <Icon size={compact ? 12 : 13} strokeWidth={2.6} className={status === "running" ? "spin" : ""} aria-hidden="true" />
      {!compact && (label ?? STATUS_LABEL[status])}
      {compact && <span className="sr-only">{label ?? STATUS_LABEL[status]}</span>}
    </span>
  );
}

/** Just the round icon, for dense places (calendar, bars). */
export function StatusDot({ status }: { status: RunStatus }) {
  const Icon = ICON[status];
  return (
    <span className={`mon-dot mon-${status}`} title={STATUS_LABEL[status]}>
      <Icon size={10} strokeWidth={3} aria-hidden="true" />
    </span>
  );
}
