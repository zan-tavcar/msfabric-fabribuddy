import { useMemo, useState } from "react";
import { ArrowLeft, Boxes, Database, FileText, Users } from "lucide-react";
import type { Report, SemanticModel, TenantData } from "../api/types";
import type { Derived, WorkspaceStat } from "../lib/insights";
import type { AccessIndex } from "../lib/access";
import { Card, StatTile } from "../components/widgets";
import { DataTable, StatusPill, type Column } from "../components/DataTable";
import { activityPill } from "../lib/pills";
import { num } from "../lib/format";

/**
 * One workspace, opened up.
 *
 * The columns here are the ones a domain owner asks about when deciding
 * whether a workspace can be archived or needs an owner: when each report was
 * last opened, whether each model still refreshes and how it stores data, and
 * who can actually see the content.
 */
export function WorkspaceDetail({
  stat,
  data,
  derived,
  access,
  onBack,
  onOpenReport,
}: {
  stat: WorkspaceStat;
  data: TenantData;
  derived: Derived;
  access: AccessIndex | null;
  onBack: () => void;
  onOpenReport: (reportId: string) => void;
}) {
  const [tab, setTab] = useState<"reports" | "models" | "fabric" | "people">("reports");
  const ws = stat.ws;

  const reports = useMemo(
    () => data.reports.filter((r) => r.ws === ws.name),
    [data.reports, ws.name],
  );
  const models = useMemo(
    () => data.models.filter((m) => m.ws === ws.name),
    [data.models, ws.name],
  );
  const members = useMemo(() => {
    const groupByName = new Map(
      (access?.principalList ?? [])
        .filter((p) => p.principal.type === "Group")
        .map((p) => [p.principal.name.toLowerCase(), p.principal.graphId]),
    );
    return Object.entries(ws.roles ?? {}).map(([who, role]) => ({
      who,
      role,
      graphId: groupByName.get(who.toLowerCase()) ?? "",
      isGroup: groupByName.has(who.toLowerCase()),
      lastSeen: derived.lastSeenByUser.get(who.toLowerCase()) ?? null,
    }));
  }, [ws.roles, access, derived.lastSeenByUser]);



  const accessByName = useMemo(() => {
    const m = new Map<string, { viewers: number; id: string }>();
    for (const r of access?.reportList ?? []) {
      if (r.wsName === ws.name) m.set(r.reportName, { viewers: r.viewers, id: r.reportId });
    }
    return m;
  }, [access, ws.name]);

  // Fabric items (lakehouses, notebooks, pipelines…) come from the permission
  // scan; a pipeline run is as much "activity" as a report view.
  const fabricItems = useMemo(
    () => (access?.fabricItems ?? []).filter((i) => i.wsName === ws.name),
    [access, ws.name],
  );

  const fabricCols: Column<(typeof fabricItems)[number]>[] = [
    {
      key: "name",
      label: "Item",
      value: (i) => i.name.toLowerCase(),
      render: (i) => <span className="strong">{i.name}</span>,
    },
    {
      key: "kind",
      label: "Type",
      width: "200px",
      value: (i) => i.kind,
      render: (i) => <StatusPill tone="neutral">{FABRIC_LABELS[i.kind] ?? i.kind}</StatusPill>,
    },
    {
      key: "modified",
      label: "Last activity",
      width: "230px",
      value: (i) =>
        activityPill(
          derived.lastSeenByItem.get(i.name.toLowerCase()) ?? i.modifiedDateTime ?? null,
        ).sort,
      render: (i) =>
        stamp(derived.lastSeenByItem.get(i.name.toLowerCase()) ?? i.modifiedDateTime ?? null),
    },
    {
      key: "grants",
      label: "Direct grants",
      width: "140px",
      value: (i) => i.users.length,
      render: (i) => <span className="tnum">{num(i.users.length)}</span>,
    },
  ];

  /** Every item gets the same treatment: a pill plus the exact timestamp. */
  const stamp = (ts: string | null) => {
    const p = activityPill(ts);
    return (
      <>
        <StatusPill tone={p.tone}>{p.label}</StatusPill>
        {ts && <div className="muted-note">{new Date(ts).toLocaleString()}</div>}
      </>
    );
  };

  const reportCols: Column<Report>[] = [
    {
      key: "name",
      label: "Report",
      value: (r) => r.name.toLowerCase(),
      render: (r) => <span className="strong link-like">{r.name}</span>,
    },
    {
      key: "model",
      label: "Built on",
      width: "230px",
      value: (r) => (r.model || "").toLowerCase(),
      render: (r) => <span className="muted-note">{r.model || "—"}</span>,
    },
    {
      key: "opened",
      label: "Last activity",
      width: "210px",
      value: (r) => activityPill(derived.lastSeenByItem.get(r.name.toLowerCase()) ?? null).sort,
      render: (r) => stamp(derived.lastSeenByItem.get(r.name.toLowerCase()) ?? null),
    },
    {
      key: "viewers",
      label: "Can view",
      width: "130px",
      value: (r) => accessByName.get(r.name)?.viewers ?? -1,
      render: (r) => {
        const a = accessByName.get(r.name);
        if (!a) return <span className="muted-note">not scanned</span>;
        return a.viewers === 0 ? (
          <StatusPill tone="warning">nobody</StatusPill>
        ) : (
          <span className="tnum">{num(a.viewers)}</span>
        );
      },
    },
  ];

  const modelCols: Column<SemanticModel>[] = [
    {
      key: "name",
      label: "Semantic model",
      value: (m) => m.name.toLowerCase(),
      render: (m) => <span className="strong">{m.name}</span>,
    },
    {
      key: "seen",
      label: "Last activity",
      width: "210px",
      value: (m) => activityPill(derived.lastSeenByItem.get(m.name.toLowerCase()) ?? null).sort,
      render: (m) => stamp(derived.lastSeenByItem.get(m.name.toLowerCase()) ?? null),
    },
    {
      key: "reports",
      label: "Reports on it",
      width: "130px",
      value: (m) => reports.filter((r) => r.model === m.name).length,
      render: (m) => {
        const n = reports.filter((r) => r.model === m.name).length;
        return n === 0 ? (
          <StatusPill tone="warning">none</StatusPill>
        ) : (
          <span className="tnum">{num(n)}</span>
        );
      },
    },
    {
      key: "by",
      label: "Configured by",
      width: "240px",
      value: (m) => (m.configuredBy || "").toLowerCase(),
      render: (m) => <span className="muted-note">{m.configuredBy || "—"}</span>,
    },
  ];

  const peopleCols: Column<(typeof members)[number]>[] = [
    {
      key: "who",
      label: "Principal",
      value: (p) => p.who.toLowerCase(),
      render: (p) => <span className="strong">{p.who}</span>,
    },
    {
      key: "role",
      label: "Workspace role",
      width: "180px",
      value: (p) => p.role,
      render: (p) => (
        <StatusPill tone={p.role === "Admin" ? "good" : "neutral"}>{p.role}</StatusPill>
      ),
    },
    {
      key: "type",
      label: "Type",
      width: "130px",
      value: (p) => (p.isGroup ? "Group" : "User"),
      render: (p) =>
        p.isGroup ? (
          <StatusPill tone="warning">Entra group</StatusPill>
        ) : (
          <StatusPill tone="neutral">User</StatusPill>
        ),
    },
    {
      key: "seen",
      label: "Last activity",
      width: "210px",
      value: (p) => activityPill(p.lastSeen).sort,
      render: (p) => stamp(p.lastSeen),
    },
    {
      key: "means",
      label: "What that means",
      sortable: false,
      render: (p) => (
        <span className="muted-note">
          {ROLE_MEANING[p.role] ?? "Access to this workspace's content."}
        </span>
      ),
    },
  ];

  const lastActivity = activityPill(stat.lastSeen);

  return (
    <div className="stack">
      <button className="btn btn-ghost back-link" onClick={onBack}>
        <ArrowLeft size={14} strokeWidth={2.2} /> All workspaces
      </button>

      <header className="page-head">
        <h2 className="page-head-title">{ws.name}</h2>
        <div className="tag-row" style={{ marginTop: 8 }}>
          <StatusPill tone={lastActivity.tone}>{lastActivity.label}</StatusPill>
          {stat.lastSeen && (
            <span className="muted-note">
              {new Date(stat.lastSeen).toLocaleString()}
              {stat.lastSeenFrom && ` · from ${stat.lastSeenFrom}`}
            </span>
          )}
          {stat.admins.length === 0 && !ws.isPersonal && (
            <StatusPill tone="critical">No admin</StatusPill>
          )}
          {stat.admins.length === 1 && !ws.isPersonal && (
            <StatusPill tone="warning">Sole admin: {stat.admins[0]}</StatusPill>
          )}
        </div>
      </header>

      <div className="kpi-row">
        <StatTile label="Reports" value={num(reports.length)} />
        <StatTile label="Semantic models" value={num(models.length)} />
        <StatTile
          label="Members"
          value={num(members.length)}
          caption={`${stat.admins.length} admins`}
        />
        <StatTile
          label={`Events (${derived.windowDays}d)`}
          value={num(stat.events)}
          caption={stat.lastSeen ? lastActivity.label.toLowerCase() : "none recorded"}
        />
      </div>

      <Card
        title="Inside this workspace"
        right={
          <div className="segmented">
            <button className={tab === "reports" ? "seg active" : "seg"} onClick={() => setTab("reports")}>
              <FileText size={13} strokeWidth={2.2} /> Reports
            </button>
            <button className={tab === "models" ? "seg active" : "seg"} onClick={() => setTab("models")}>
              <Database size={13} strokeWidth={2.2} /> Models
            </button>
            {fabricItems.length > 0 && (
              <button className={tab === "fabric" ? "seg active" : "seg"} onClick={() => setTab("fabric")}>
                <Boxes size={13} strokeWidth={2.2} /> Fabric ({fabricItems.length})
              </button>
            )}
            <button className={tab === "people" ? "seg active" : "seg"} onClick={() => setTab("people")}>
              <Users size={13} strokeWidth={2.2} /> People
            </button>
          </div>
        }
      >
        {tab === "reports" && (
          <DataTable
            columns={reportCols}
            rows={reports}
            rowKey={(r) => r.id || r.name}
            initialSort="opened"
            initialDir="asc"
            onRowClick={(r) => {
              const a = accessByName.get(r.name);
              if (a) onOpenReport(a.id);
            }}
            empty="No reports in this workspace."
          />
        )}
        {tab === "models" && (
          <DataTable
            columns={modelCols}
            rows={models}
            rowKey={(m) => m.id || m.name}
            initialSort="refresh"
            initialDir="asc"
            empty="No semantic models in this workspace."
          />
        )}
        {tab === "fabric" && (
          <DataTable
            columns={fabricCols}
            rows={fabricItems}
            rowKey={(i) => i.id || `${i.kind}-${i.name}`}
            initialSort="modified"
            initialDir="desc"
            empty="No Fabric items in this workspace."
          />
        )}
        {tab === "people" && (
          <DataTable
            columns={peopleCols}
            rows={members}
            rowKey={(p) => p.who}
            initialSort="seen"
            initialDir="asc"
            empty="Nobody has a role on this workspace."
            canExpand={(p) => p.isGroup && (access?.groupMembers?.[p.graphId]?.length ?? 0) > 0}
            expand={(p) => {
              const list = access?.groupMembers?.[p.graphId] ?? [];
              return (
                <div className="nested">
                  <div className="nested-title">{num(list.length)} people in {p.who}</div>
                  <ul className="nested-list">
                    {list.map((m) => (
                      <li key={m.id || m.upn || m.name}>
                        {m.name}
                        {m.upn && <span className="muted-note"> · {m.upn}</span>}
                      </li>
                    ))}
                  </ul>
                </div>
              );
            }}
          />
        )}
      </Card>
    </div>
  );
}

const FABRIC_LABELS: Record<string, string> = {
  Lakehouse: "Lakehouse",
  warehouses: "Warehouse",
  Notebook: "Notebook",
  DataPipeline: "Data pipeline",
  Eventstream: "Eventstream",
  EventSubscription: "Event subscription",
  SQLAnalyticsEndpoint: "SQL analytics endpoint",
  SQLDatabase: "SQL database",
  MirroredDatabase: "Mirrored database",
  GraphQLApi: "GraphQL API",
  MountedDataFactory: "Mounted data factory",
  VariableLibrary: "Variable library",
  Reflex: "Reflex (Activator)",
  Dataflow: "Dataflow",
};

const ROLE_MEANING: Record<string, string> = {
  Admin: "Full control, including adding and removing members.",
  Member: "Can publish, share and edit all content here.",
  Contributor: "Can create and edit content, but not share it onward.",
  Viewer: "Read-only on content they are also granted the model for.",
};
