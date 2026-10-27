/**
 * Status pills: a tinted capsule carrying a state and a relative time.
 *
 * The tone is never the only signal — the label always spells the state out
 * ("Failed, 3 days", "No refresh, 19mo"), so the meaning survives colour-blind
 * vision, greyscale print and forced-colors.
 */
export type Tone = "good" | "warning" | "critical" | "neutral";

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

function ageMs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, Date.now() - t);
}

/** "Today", "2 days ago", "14 months ago" — the Workspaces column. */
export function agoLabel(ms: number): string {
  if (ms < DAY) return "Today";
  const days = Math.floor(ms / DAY);
  if (days === 1) return "Yesterday";
  if (days < 30) return `${days} days ago`;
  const months = Math.floor(days / 30);
  if (months < 24) return `${months} month${months === 1 ? "" : "s"} ago`;
  return `${Math.floor(months / 12)} years ago`;
}

/** Compact form for inside a two-part pill: "6h", "3 days", "19mo". */
export function agoShort(ms: number): string {
  if (ms < HOUR) return `${Math.max(1, Math.floor(ms / MIN))}m`;
  if (ms < DAY) return `${Math.floor(ms / HOUR)}h`;
  const days = Math.floor(ms / DAY);
  if (days < 30) return `${days} day${days === 1 ? "" : "s"}`;
  const months = Math.floor(days / 30);
  if (months < 24) return `${months}mo`;
  return `${Math.floor(months / 12)}y`;
}

/**
 * Last-activity pill. Recent is good, a quarter is worth a look, and anything
 * past six months is the abandoned-content case this product exists to find.
 */
export function activityPill(iso: string | null | undefined): {
  tone: Tone;
  label: string;
  sort: number;
} {
  const ms = ageMs(iso);
  if (ms === null) return { tone: "neutral", label: "No activity", sort: -1 };
  const days = ms / DAY;
  const tone: Tone = days <= 30 ? "good" : days <= 180 ? "warning" : "critical";
  return { tone, label: agoLabel(ms), sort: Date.now() - ms };
}

/**
 * Refresh pill: "Ok, 6h ago" / "Failed, 3 days" / "No refresh, 19mo".
 * A dataset with no refresh record is `neutral`, never green — unknown is not
 * the same as healthy.
 */
export function refreshPill(
  status: string | undefined,
  endTime: string | undefined,
  fallbackSeen?: string | null,
): { tone: Tone; label: string; sort: number } {
  const ms = ageMs(endTime) ?? ageMs(fallbackSeen);
  const ok = (status || "").toLowerCase() === "completed";
  const failed = Boolean(status) && !ok;

  if (!status && ms === null) {
    return { tone: "neutral", label: "No refresh data", sort: -1 };
  }
  if (!status && ms !== null) {
    // Seen in the audit log but no status from the refreshables API.
    return { tone: "neutral", label: `Ran ${agoShort(ms)} ago`, sort: Date.now() - ms };
  }
  if (failed) {
    return {
      tone: "critical",
      label: ms === null ? "Failed" : `Failed, ${agoShort(ms)}`,
      sort: ms === null ? 0 : Date.now() - ms,
    };
  }
  if (ms !== null && ms > 30 * DAY) {
    return { tone: "warning", label: `No refresh, ${agoShort(ms)}`, sort: Date.now() - ms };
  }
  return {
    tone: "good",
    label: ms === null ? "Ok" : `Ok, ${agoShort(ms)} ago`,
    sort: ms === null ? 0 : Date.now() - ms,
  };
}

/** Import / DirectQuery / Direct Lake, from the scanner API's raw value. */
export function storageModeLabel(raw: string | undefined): string {
  const v = (raw || "").toLowerCase();
  if (!v) return "—";
  if (v === "abf") return "Import";
  if (v.includes("directlake")) return "Direct Lake";
  if (v.includes("directquery")) return "DirectQuery";
  if (v.includes("premiumfiles")) return "Large model";
  return raw as string;
}
