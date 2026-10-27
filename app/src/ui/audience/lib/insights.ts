/**
 * Turns a raw scan into the questions a Power BI domain owner actually asks.
 *
 * The POC showed inventory: here are your workspaces, here are your reports.
 * Inventory is not a finding. Everything below exists to answer a question
 * someone would ask out loud — "am I paying for seats nobody uses?", "which
 * workspace is nobody's job?" — and each finding carries the evidence behind
 * its number so it can be defended in a meeting.
 *
 * Honest limits, surfaced in the UI rather than hidden:
 *  - Activity retention is ~30 days, so "dormant" means "silent in the window",
 *    never "abandoned for a year". CLAUDE.md's 1-year abandoned-workspace flag
 *    needs a longer rolling store than the API gives us today.
 *  - Activity events identify people by UPN; the admin scan identifies them by
 *    display name. Matching falls back across both, and can miss.
 */
import type {
  Activity,
  LicenseRow,
  Principal,
  Report,
  SemanticModel,
  TenantData,
  Workspace,
} from "../api/types";

export type Severity = "critical" | "serious" | "warning" | "good";

export interface Finding {
  id: string;
  severity: Severity;
  /** The question a domain owner would ask. */
  question: string;
  /** The answer, as a headline. */
  headline: string;
  detail: string;
  count: number;
  /** Denominator, when the count is a share of something. */
  total?: number;
  evidence: string[];
  goTo?: ViewId;
}

export type ViewId =
  | "overview"
  | "licenses"
  | "workspaces"
  | "content"
  | "people"
  | "activity"
  | "access"
  | "settings"
  | "platform";

export interface WorkspaceStat {
  ws: Workspace;
  reports: number;
  models: number;
  users: number;
  admins: string[];
  events: number;
  /** Rolled up from the workspace's own events AND everything inside it. */
  lastSeen: string | null;
  /** What produced that timestamp, so the number can be defended. */
  lastSeenFrom: string;
  dormant: boolean;
}

export interface PeopleStat {
  name: string;
  principalType: string;
  workspaces: number;
  adminOf: number;
  reachableReports: number;
  events: number;
  exports: number;
  lastSeen: string | null;
  license: "Pro" | "PPU" | null;
  unusedSeat: boolean;
}

export interface Derived {
  findings: Finding[];
  workspaceStats: WorkspaceStat[];
  peopleStats: PeopleStat[];
  dormantReports: Report[];
  activeReportNames: Set<string>;
  orphanModels: SemanticModel[];
  daily: { date: string; views: number; other: number; users: number; viewers: number }[];
  actionMix: { action: string; count: number }[];
  lastSeenByReport: Map<string, string>;
  /** Any interaction, not just views — used for licences and workspaces. */
  lastSeenByItem: Map<string, string>;
  lastSeenByUser: Map<string, string>;
  lastRefreshByDataset: Map<string, string>;
  /** Distinct PEOPLE per day — the honest "is Power BI being used" signal. */
  dailyUsers: { date: string; users: number; human: number }[];
  activityTypes: string[];
  /** True when we have activity to reason about at all. */
  activityKnown: boolean;
  windowDays: number;
}

const norm = (s: string | null | undefined) => (s ?? "").trim().toLowerCase();

export function derive(
  data: TenantData,
  fabricItems: { wsName: string; name: string; modifiedDateTime: string }[] = [],
): Derived {
  const { workspaces, reports, models, users, activity, licenses } = data;
  const activityKnown = activity.live && activity.summary.total > 0;
  const windowDays = activity.summary.days || 30;

  // --- activity indexes ----------------------------------------------------
  // These come from server-side aggregates over EVERY event in the window.
  // `activity.events` is a bounded sample for the feed; deriving counts from it
  // would silently under-report on any busy tenant.
  const lower = (o: Record<string, number> | undefined) =>
    new Map(Object.entries(o ?? {}).map(([k, v]) => [norm(k), v]));
  const lowerStr = (o: Record<string, string> | undefined) =>
    new Map(Object.entries(o ?? {}).map(([k, v]) => [norm(k), v]));

  const activeReportNames = new Set(
    (activity.touchedReports ?? []).map((r) => norm(r)),
  );
  const lastSeenByReport = lowerStr(activity.lastSeenByReport);
  const lastSeenByItem = lowerStr(activity.lastSeenByItem);
  const lastRefreshByDataset = lowerStr(activity.lastRefreshByDataset);
  const wsEventCount = lower(activity.eventsByWorkspace);
  const wsLastSeen = lowerStr(activity.lastSeenByWorkspace);
  const userEvents = lower(activity.eventsByUser);
  const userExports = lower(activity.exportsByUser);
  const userLastSeen = lowerStr(activity.lastSeenByUser);

  const dayBuckets = new Map<
    string,
    { views: number; other: number; users: number; viewers: number }
  >();
  for (const d of activity.daily ?? []) {
    dayBuckets.set(d.date, {
      views: d.views, other: d.other ?? 0, users: d.users ?? 0, viewers: d.viewers ?? 0,
    });
  }

  // Fill every day in the window so the trend never fakes continuity by
  // skipping silent days.
  const daily = buildDailySeries(dayBuckets, windowDays);

  const actionMix = (activity.actionMix ?? []).map((a) => ({
    action: a.action,
    count: a.count,
  }));

  // --- per-workspace -------------------------------------------------------
  // Fabric item modifications count as activity too.
  const fabricByWs = new Map<string, { name: string; ts: string }[]>();
  for (const f of fabricItems) {
    if (!f.modifiedDateTime) continue;
    const k = norm(f.wsName);
    if (!fabricByWs.has(k)) fabricByWs.set(k, []);
    fabricByWs.get(k)!.push({ name: f.name, ts: f.modifiedDateTime });
  }

  const workspaceStats: WorkspaceStat[] = workspaces.map((ws) => {
    const key = norm(ws.name);
    const admins = Object.entries(ws.roles ?? {})
      .filter(([, role]) => norm(role) === "admin")
      .map(([who]) => who);
    const events = wsEventCount.get(key) ?? 0;

    // The audit log leaves WorkspaceName empty on most events, so a workspace
    // looked dormant while its own reports plainly had views. Roll the answer
    // up from everything inside it instead of trusting the workspace field.
    let best = wsLastSeen.get(key) ?? null;
    let from = best ? "workspace event" : "";
    const consider = (ts: string | null | undefined, label: string) => {
      if (ts && (!best || ts > best)) {
        best = ts;
        from = label;
      }
    };
    for (const r of ws.reports ?? []) {
      consider(lastSeenByItem.get(norm(r)), `report "${r}"`);
    }
    for (const m of ws.models ?? []) {
      consider(lastSeenByItem.get(norm(m)), `semantic model "${m}"`);
    }
    for (const f of fabricByWs.get(key) ?? []) {
      consider(f.ts, `Fabric item "${f.name}"`);
    }

    return {
      ws,
      reports: ws.reports?.length ?? 0,
      models: ws.models?.length ?? 0,
      users: ws.users?.length ?? 0,
      admins,
      events,
      lastSeen: best,
      lastSeenFrom: from,
      dormant: activityKnown && !best,
    };
  });

  // --- per-person ----------------------------------------------------------
  const licenseByName = new Map<string, LicenseRow>();
  for (const row of licenses.rows) licenseByName.set(norm(row.name), row);

  const peopleStats: PeopleStat[] = users.map((u) => {
    const lic = licenseByName.get(norm(u.name));
    const events = lookupPerson(userEvents, u);
    return {
      name: u.name,
      principalType: u.principalType || "User",
      workspaces: u.workspaces?.length ?? 0,
      adminOf: (u.workspaces ?? []).filter(([, r]) => norm(r) === "admin").length,
      reachableReports: u.reports?.length ?? 0,
      events,
      exports: lookupPerson(userExports, u),
      lastSeen: lookupPersonStr(userLastSeen, u),
      license: lic?.license ?? null,
      unusedSeat: Boolean(lic?.unused),
    };
  });

  // --- content -------------------------------------------------------------
  const dormantReports = activityKnown
    ? reports.filter((r) => !activeReportNames.has(norm(r.name)))
    : [];

  const modelsWithReports = new Set(reports.map((r) => norm(r.model)));
  const orphanModels = models.filter(
    (m) => m.name && !modelsWithReports.has(norm(m.name)),
  );

  const findings = buildFindings({
    workspaces,
    workspaceStats,
    peopleStats,
    reports,
    dormantReports,
    orphanModels,
    licenses,
    activity,
    activityKnown,
    windowDays,
  });

  return {
    findings,
    workspaceStats,
    peopleStats,
    dormantReports,
    activeReportNames,
    orphanModels,
    daily,
    actionMix,
    lastSeenByReport,
    lastSeenByItem,
    lastSeenByUser: userLastSeen,
    lastRefreshByDataset,
    dailyUsers: (activity.daily ?? []).map((d) => ({
      date: d.date,
      users: (d as { users?: number }).users ?? 0,
      human: (d as { human?: number }).human ?? 0,
    })),
    activityTypes: activity.activityTypes ?? [],
    activityKnown,
    windowDays,
  };
}

/** Activity names people by UPN; the scan names them by display name. Try both. */
function lookupPerson(map: Map<string, number>, u: Principal): number {
  return map.get(norm(u.identifier)) ?? map.get(norm(u.name)) ?? 0;
}
function lookupPersonStr(map: Map<string, string>, u: Principal): string | null {
  return map.get(norm(u.identifier)) ?? map.get(norm(u.name)) ?? null;
}

function buildDailySeries(
  buckets: Map<string, { views: number; other: number; users: number; viewers: number }>,
  days: number,
) {
  const out: Derived["daily"] = [];
  const today = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setUTCDate(d.getUTCDate() - i);
    const key = d.toISOString().slice(0, 10);
    const b = buckets.get(key) ?? { views: 0, other: 0, users: 0, viewers: 0 };
    out.push({ date: key, ...b });
  }
  return out;
}

interface FindingInput {
  workspaces: Workspace[];
  workspaceStats: WorkspaceStat[];
  peopleStats: PeopleStat[];
  reports: Report[];
  dormantReports: Report[];
  orphanModels: SemanticModel[];
  licenses: TenantData["licenses"];
  activity: Activity;
  activityKnown: boolean;
  windowDays: number;
}

function buildFindings(i: FindingInput): Finding[] {
  const out: Finding[] = [];
  const {
    workspaceStats,
    peopleStats,
    reports,
    dormantReports,
    orphanModels,
    licenses,
    activityKnown,
    windowDays,
  } = i;

  // 1. Money on the table — the core sales hook.
  if (licenses.live && licenses.summary.activityJoined) {
    const unused = licenses.summary.unused;
    out.push({
      id: "unused-seats",
      severity: unused === 0 ? "good" : unused > 5 ? "critical" : "serious",
      question: "Am I paying for seats nobody uses?",
      headline:
        unused === 0
          ? "Every paid seat was used"
          : `${unused} paid ${plural(unused, "seat")} went unused`,
      detail:
        unused === 0
          ? `All ${licenses.summary.total} Pro and PPU seats show activity in the last ${windowDays} days.`
          : `${unused} of ${licenses.summary.total} Pro/PPU seats recorded no Power BI activity in the last ${windowDays} days. Each is a licence you can reclaim or downgrade.`,
      count: unused,
      total: licenses.summary.total,
      evidence: licenses.rows
        .filter((r) => r.unused)
        .slice(0, 6)
        .map((r) => `${r.name} · ${r.license}`),
      goTo: "licenses",
    });
  } else if (licenses.live) {
    out.push({
      id: "seats-unjoined",
      severity: "warning",
      question: "Am I paying for seats nobody uses?",
      headline: `${licenses.summary.total} paid seats, usage unknown`,
      detail:
        "Licences were read, but there is no activity data to compare them against, so no seat can be called unused. Enable the audit log so this becomes answerable.",
      count: licenses.summary.total,
      evidence: [],
      goTo: "licenses",
    });
  }

  // 2. Content nobody opens.
  if (activityKnown && reports.length > 0) {
    const n = dormantReports.length;
    const share = Math.round((n / reports.length) * 100);
    out.push({
      id: "dormant-reports",
      severity: share >= 70 ? "serious" : share >= 40 ? "warning" : "good",
      question: "How much of what we built is anyone actually opening?",
      headline: `${n} of ${reports.length} reports were never opened`,
      detail: `${share}% of reports recorded no views in the last ${windowDays} days. Long-dormant reports still cost refresh capacity and still expose data.`,
      count: n,
      total: reports.length,
      evidence: dormantReports.slice(0, 6).map((r) => `${r.name} · ${r.ws}`),
      goTo: "content",
    });
  }

  // 3. Business content in personal workspaces — the classic governance gap.
  const personalWithContent = workspaceStats.filter(
    (s) => s.ws.isPersonal && s.reports + s.models > 0,
  );
  if (personalWithContent.length > 0) {
    const items = personalWithContent.reduce(
      (a, s) => a + s.reports + s.models,
      0,
    );
    out.push({
      id: "personal-workspace-content",
      severity: items > 20 ? "serious" : "warning",
      question: "Is business content sitting in someone's personal workspace?",
      headline: `${items} ${plural(items, "item")} live in personal workspaces`,
      detail: `Across ${personalWithContent.length} personal ${plural(personalWithContent.length, "workspace")}. Content here has no shared ownership — it leaves with the person and is invisible to your normal review.`,
      count: items,
      evidence: personalWithContent
        .slice(0, 6)
        .map((s) => `${s.ws.name} · ${s.reports + s.models} items`),
      goTo: "workspaces",
    });
  }

  // 4. Ownership risk — who is responsible when this breaks?
  const noAdmin = workspaceStats.filter(
    (s) => !s.ws.isPersonal && s.admins.length === 0 && s.users > 0,
  );
  const soleAdmin = workspaceStats.filter(
    (s) => !s.ws.isPersonal && s.admins.length === 1,
  );
  if (noAdmin.length > 0) {
    out.push({
      id: "no-admin",
      severity: "critical",
      question: "Which workspaces are nobody's job?",
      headline: `${noAdmin.length} ${plural(noAdmin.length, "workspace")} with no admin`,
      detail:
        "Nobody can grant access, fix a broken refresh, or answer for what is in them. This is the first thing an auditor asks about.",
      count: noAdmin.length,
      evidence: noAdmin.slice(0, 6).map((s) => s.ws.name),
      goTo: "workspaces",
    });
  }
  if (soleAdmin.length > 0) {
    out.push({
      id: "sole-admin",
      severity: "warning",
      question: "What happens if one person leaves?",
      headline: `${soleAdmin.length} ${plural(soleAdmin.length, "workspace")} ${soleAdmin.length === 1 ? "depends" : "depend"} on a single admin`,
      detail:
        "One person is the only one who can administer these. If they leave, access has to be recovered by a tenant admin.",
      count: soleAdmin.length,
      evidence: soleAdmin
        .slice(0, 6)
        .map((s) => `${s.ws.name} · ${s.admins[0]}`),
      goTo: "workspaces",
    });
  }

  // 5. Dormant workspaces.
  if (activityKnown) {
    const dormant = workspaceStats.filter((s) => s.dormant && !s.ws.isPersonal);
    if (dormant.length > 0) {
      out.push({
        id: "dormant-workspaces",
        severity: dormant.length > 5 ? "serious" : "warning",
        question: "Which workspaces can we archive?",
        headline: `${dormant.length} ${plural(dormant.length, "workspace")} saw no activity`,
        detail: `No recorded events in the last ${windowDays} days. Candidates for archiving — confirm against a longer window before deleting anything.`,
        count: dormant.length,
        evidence: dormant
          .slice(0, 6)
          .map((s) => `${s.ws.name} · ${s.reports} reports`),
        goTo: "workspaces",
      });
    }
  }

  // 6. Reach — who can see the most?
  const broad = peopleStats
    .filter((p) => p.principalType !== "Group" && p.reachableReports > 0)
    .sort((a, b) => b.reachableReports - a.reachableReports);
  if (broad.length > 0 && reports.length > 0) {
    const top = broad[0];
    const share = Math.round((top.reachableReports / reports.length) * 100);
    if (share >= 50) {
      out.push({
        id: "broad-reach",
        severity: share >= 90 ? "serious" : "warning",
        question: "Who can see the most, and should they?",
        headline: `${top.name} can reach ${share}% of all reports`,
        detail: `${top.reachableReports} of ${reports.length} reports, through ${top.workspaces} ${plural(top.workspaces, "workspace")}. Worth confirming that breadth is intentional.`,
        count: top.reachableReports,
        total: reports.length,
        evidence: broad
          .slice(0, 6)
          .map((p) => `${p.name} · ${p.reachableReports} reports`),
        goTo: "people",
      });
    }
  }

  // 7. Bulk export — the data-exfiltration signal.
  // Taken straight from the activity aggregate rather than from matched
  // principals, so a display-name mismatch cannot hide an exporter.
  const exporters = Object.entries(i.activity.exportsByUser ?? {})
    .map(([who, n]) => ({ name: who, exports: n }))
    .filter((p) => p.exports > 0)
    .sort((a, b) => b.exports - a.exports);
  if (exporters.length > 0) {
    const total = exporters.reduce((a, p) => a + p.exports, 0);
    out.push({
      id: "exports",
      severity: total > 50 ? "serious" : "warning",
      question: "Is data leaving Power BI in bulk?",
      headline: `${total} ${plural(total, "export")} by ${exporters.length} ${plural(exporters.length, "person", "people")}`,
      detail: `Exports move data out of the governed surface into spreadsheets you cannot audit. Worth a look when one person accounts for most of them.`,
      count: total,
      evidence: exporters
        .slice(0, 6)
        .map((p) => `${p.name} · ${p.exports} exports`),
      goTo: "people",
    });
  }

  // 8. Semantic models with nothing built on them.
  if (orphanModels.length > 0) {
    out.push({
      id: "orphan-models",
      severity: "warning",
      question: "Are we refreshing models nothing depends on?",
      headline: `${orphanModels.length} semantic ${plural(orphanModels.length, "model")} have no reports`,
      detail:
        "No report in the tenant is built on these. If they still refresh on a schedule, they are burning capacity for nothing.",
      count: orphanModels.length,
      evidence: orphanModels.slice(0, 6).map((m) => `${m.name} · ${m.ws}`),
      goTo: "content",
    });
  }

  // 9. Be loud when the data behind all of the above is incomplete.
  if (i.activity.summary.partial) {
    out.push({
      id: "activity-truncated",
      severity: "serious",
      question: "Can I trust these usage numbers?",
      headline: `Activity covers ${i.activity.summary.days} of ${i.activity.summary.daysRequested ?? 30} days`,
      detail:
        `The audit-log pull stopped early, so the oldest days are missing and anything counting views or unused seats is an undercount. ${i.activity.summary.stoppedReason ? `Reason: ${i.activity.summary.stoppedReason}` : ""}`,
      count: i.activity.summary.total,
      evidence: [],
      goTo: "activity",
    });
  }

  const rank: Record<Severity, number> = {
    critical: 0,
    serious: 1,
    warning: 2,
    good: 3,
  };
  return out.sort((a, b) => rank[a.severity] - rank[b.severity]);
}

function plural(n: number, one: string, many?: string) {
  return n === 1 ? one : (many ?? `${one}s`);
}
