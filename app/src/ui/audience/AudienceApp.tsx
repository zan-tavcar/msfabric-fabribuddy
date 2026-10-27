import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity as ActivityIcon,
  BadgeCheck,
  FolderKanban,
  LayoutDashboard,
  Library,
  ShieldCheck,
  Users,
} from "lucide-react";
import { ApiError } from "../api/client";
import { Empty, Loading } from "../components/primitives";
import { audienceApi } from "./api/client";
import type { AudienceStatus, PermissionScan, TenantData } from "./api/types";
import { buildAccessIndex } from "./lib/access";
import { derive, type ViewId, type WorkspaceStat } from "./lib/insights";
import { Access } from "./views/Access";
import { ActivityView } from "./views/ActivityView";
import { Licenses } from "./views/Licenses";
import { ModelsAndReports } from "./views/ModelsAndReports";
import { Overview } from "./views/Overview";
import { People } from "./views/People";
import { WorkspaceDetail } from "./views/WorkspaceDetail";
import { Workspaces } from "./views/Workspaces";

export type AudienceView = ViewId;

export const AUDIENCE_NAV: { id: ViewId; label: string; icon: typeof LayoutDashboard }[] = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "access", label: "Who sees what", icon: ShieldCheck },
  { id: "people", label: "People", icon: Users },
  { id: "activity", label: "Activity", icon: ActivityIcon },
  { id: "licenses", label: "Licences", icon: BadgeCheck },
  { id: "workspaces", label: "Workspaces", icon: FolderKanban },
  { id: "content", label: "Models & reports", icon: Library },
];

/** What the shared top bar shows while Audience is open. */
export interface AudienceMeta {
  lastScan: string | null;
  workspaces: number | null;
  reports: number | null;
  busy: boolean;
}

// The audit log retains ~30 days, so that is simply "all of it".
const DAYS = 30;

/**
 * FabriBuddy Report Audience inside the app shell: who can see which Power BI
 * report, who opens it, which seats nobody uses. Reads the stored scans only;
 * the shell's Rescan (or the schedule) is what scans.
 */
export function AudienceApp({
  view,
  onNavigate,
  admin,
  kick,
  onMeta,
  onRescan,
}: {
  view: ViewId;
  onNavigate: (view: ViewId) => void;
  admin: boolean;
  /** Bumped by the shell after it starts a scan, so polling starts now. */
  kick: number;
  onMeta: (meta: AudienceMeta) => void;
  onRescan: () => void;
}) {
  const [status, setStatus] = useState<AudienceStatus | null>(null);
  const [data, setData] = useState<TenantData | null>(null);
  const [perm, setPerm] = useState<PermissionScan | null>(null);
  const [booting, setBooting] = useState(true);
  const [needsTenant, setNeedsTenant] = useState(false);
  const [notScanned, setNotScanned] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [openWorkspace, setOpenWorkspace] = useState<WorkspaceStat | null>(null);
  const [pendingReport, setPendingReport] = useState<string | null>(null);
  const [pendingPerson, setPendingPerson] = useState<string | null>(null);

  /** Inventory first (the page is usable), then activity and licences, then permissions. */
  const load = useCallback(async () => {
    try {
      setStatus(await audienceApi.status());
      setNeedsTenant(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setNeedsTenant(true);
        return;
      }
      throw e;
    }
    try {
      setData(await audienceApi.loadCore());
      setNotScanned(false);
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setNotScanned(true);
        return;
      }
      throw e;
    }
    const [activity, licenses] = await Promise.all([
      audienceApi.loadActivity(DAYS),
      audienceApi.loadLicenses(DAYS),
    ]);
    setData((d) => (d ? { ...d, activity, licenses } : d));
    try {
      const p = await audienceApi.getPermissions();
      setPerm(p.scanned ? (p as PermissionScan) : null);
    } catch {
      setPerm(null);
    }
  }, []);

  useEffect(() => {
    load()
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)))
      .finally(() => setBooting(false));
  }, [load]);

  // Poll while a pass runs (inventory, permissions, activity, licences) and
  // refresh as each stage finishes; at an idle pace, notice scheduled scans.
  const finishedRef = useRef<string | null>(null);
  useEffect(() => {
    if (needsTenant) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const next = await audienceApi.status().catch(() => null);
      if (cancelled) return;
      if (next) {
        const finished = next.scans
          .filter((s) => !s.running)
          .map((s) => `${s.kind}@${s.lastRunAt}:${s.lastStatus}`)
          .join("|");
        const changed = finishedRef.current !== null && finishedRef.current !== finished;
        finishedRef.current = finished;
        setStatus(next);
        if (changed) await load().catch(() => undefined);
      }
      timer = setTimeout(tick, next?.anyRunning ? 4000 : 30000);
    };
    timer = setTimeout(tick, kick ? 1500 : 4000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [needsTenant, kick, load]);

  const busy = Boolean(status?.anyRunning);
  useEffect(() => {
    onMeta({
      lastScan:
        status?.scans
          .map((s) => s.lastSuccessAt)
          .filter((v): v is string => Boolean(v))
          .sort()
          .pop() ?? null,
      workspaces: data?.summary.workspaces ?? null,
      reports: data?.summary.reports ?? null,
      busy,
    });
  }, [status, data, busy, onMeta]);

  useEffect(() => {
    if (view !== "workspaces") setOpenWorkspace(null);
  }, [view]);

  const derived = useMemo(
    () => (data ? derive(data, perm?.fabricItems ?? []) : null),
    [data, perm],
  );
  const accessIndex = useMemo(() => (perm ? buildAccessIndex(perm) : null), [perm]);

  if (booting) return <Loading what="FabriBuddy Report Audience" />;
  if (error) return <p className="banner banner-danger">{error}</p>;
  if (needsTenant) {
    return (
      <Empty title="No tenant connected">
        {admin
          ? "Connect your tenant under Settings (⚙ top right) → Connection; Audience uses the same service principal as Lineage."
          : "Your organisation's admins have not connected a tenant yet. Results appear here after their first scan."}
      </Empty>
    );
  }
  // Report Audience reads only through the Power BI admin APIs; without them
  // there is nothing to scan (the in-tenant edition, run by someone who isn't
  // a Fabric administrator).
  if (notScanned && status?.tenant.capabilities?.powerbi_admin?.available === false) {
    return (
      <Empty title="Report Audience needs admin access">
        It reads who can see each report, and who opens it, through the Power BI admin APIs.
        Run the scan as a Fabric administrator, or as a service principal allowed to use the
        read-only admin APIs.
      </Empty>
    );
  }
  if (notScanned) {
    return (
      <Empty title="Not scanned yet">
        {admin
          ? busy
            ? "The first scan is running — pages fill in as each stage finishes."
            : "Run the first scan with Rescan (top right). Inventory takes seconds; the 30-day audit log takes a few minutes."
          : "Your admins have connected a tenant but not scanned it yet."}
      </Empty>
    );
  }
  if (!data || !derived) return <Loading what="your tenant" />;

  const focusReport = (id: string) => {
    setPendingReport(id);
    onNavigate("access");
  };
  const openPerson = (key: string) => {
    setPendingPerson(key);
    onNavigate("access");
  };

  switch (view) {
    case "overview":
      return <Overview data={data} derived={derived} onNavigate={onNavigate} />;
    case "licenses":
      return <Licenses data={data} derived={derived} />;
    case "workspaces":
      return openWorkspace ? (
        <WorkspaceDetail
          stat={openWorkspace}
          data={data}
          derived={derived}
          access={accessIndex}
          onBack={() => setOpenWorkspace(null)}
          onOpenReport={focusReport}
        />
      ) : (
        <Workspaces derived={derived} onOpen={setOpenWorkspace} />
      );
    case "content":
      return <ModelsAndReports data={data} derived={derived} onOpenReport={focusReport} />;
    case "access":
      return (
        <Access
          perm={perm}
          scanning={
            Boolean(status?.scans.some((s) => s.kind === "permissions" && s.running)) ||
            (busy && !perm)
          }
          onScan={onRescan}
          focusReport={pendingReport}
          focusPerson={pendingPerson}
          onFocusHandled={() => {
            setPendingReport(null);
            setPendingPerson(null);
          }}
        />
      );
    case "people":
      return <People derived={derived} access={accessIndex} onOpenPerson={openPerson} />;
    case "activity":
      return <ActivityView data={data} derived={derived} />;
    default:
      return null;
  }
}
