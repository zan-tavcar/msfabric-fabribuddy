import { useMemo, useState } from "react";
import type { Derived, WorkspaceStat } from "../lib/insights";
import { Card, StatTile } from "../components/widgets";
import { DataTable, FilterBar, StatusPill, type Column } from "../components/DataTable";
import { activityPill } from "../lib/pills";
import { num } from "../lib/format";

type Scope = "all" | "active" | "abandoned";

const SCOPES: { id: Scope; label: string }[] = [
  { id: "all", label: "All" },
  { id: "active", label: "Active" },
  { id: "abandoned", label: "Abandoned" },
];

/** Abandoned = no recorded activity in the window we can actually see. */
const isAbandoned = (s: WorkspaceStat) => !s.ws.isPersonal && !s.lastSeen;

export function Workspaces({
  derived,
  onOpen,
}: {
  derived: Derived;
  onOpen: (s: WorkspaceStat) => void;
}) {
  const [scope, setScope] = useState<Scope>("all");
  const [wsFilter, setWsFilter] = useState<string[]>([]);
  const [ownership, setOwnership] = useState<string[]>([]);
  const [kind, setKind] = useState<string[]>([]);

  const all = derived.workspaceStats;
  const abandoned = all.filter(isAbandoned);
  const ownerless = all.filter((s) => s.admins.length === 0);

  const rows = useMemo(() => {
    return all.filter((s) => {
      if (scope === "abandoned" && !isAbandoned(s)) return false;
      if (scope === "active" && isAbandoned(s)) return false;
      if (kind.length && !kind.includes(s.ws.kind ?? "Pro")) return false;
      if (wsFilter.length && !wsFilter.includes(s.ws.name)) return false;
      const ownedAs = (o: string) =>
        o === "No admin" ? s.admins.length === 0 : s.admins.length === 1;
      if (ownership.length && !ownership.some(ownedAs)) return false;
      return true;
    });
  }, [all, scope, wsFilter, ownership, kind]);

  const wsNames = useMemo(
    () => [...new Set(all.map((s) => s.ws.name))].sort().slice(0, 800),
    [all],
  );

  const columns: Column<WorkspaceStat>[] = [
    {
      key: "name",
      label: "Workspace",
      value: (s) => s.ws.name.toLowerCase(),
      render: (s) => (
        <span className="strong link-like">{s.ws.name}</span>
      ),
    },
    {
      key: "kind",
      label: "Type",
      width: "120px",
      value: (s) => s.ws.kind ?? "Pro",
      render: (s) => {
        const k = s.ws.kind ?? "Pro";
        const tone = k === "Fabric" ? "good" : k === "PPU" || k === "Premium" ? "warning" : "neutral";
        return <StatusPill tone={tone}>{k}</StatusPill>;
      },
    },
    {
      key: "models",
      label: "Models",
      width: "100px",
      value: (s) => s.models,
      render: (s) => <span className="tnum">{num(s.models)}</span>,
    },
    {
      key: "reports",
      label: "Reports",
      width: "100px",
      value: (s) => s.reports,
      render: (s) => <span className="tnum">{num(s.reports)}</span>,
    },
    {
      key: "users",
      label: "Users",
      width: "90px",
      value: (s) => s.users,
      render: (s) => <span className="tnum">{num(s.users)}</span>,
    },
    {
      key: "admins",
      label: "Admins",
      width: "170px",
      value: (s) => s.admins.length,
      render: (s) =>
        s.admins.length === 0 ? (
          <StatusPill tone="critical">No admin</StatusPill>
        ) : s.admins.length === 1 ? (
          <StatusPill tone="warning">Sole admin</StatusPill>
        ) : (
          <span className="tnum">{s.admins.length}</span>
        ),
    },
    {
      key: "activity",
      label: "Last activity",
      width: "280px",
      value: (s) => activityPill(s.lastSeen).sort,
      render: (s) => {
        const p = activityPill(s.lastSeen);
        return (
          <>
            <StatusPill tone={p.tone}>{p.label}</StatusPill>
            {s.lastSeen && (
              <div className="muted-note" title={s.lastSeenFrom}>
                {new Date(s.lastSeen).toLocaleString()}
                {s.lastSeenFrom && ` · ${s.lastSeenFrom}`}
              </div>
            )}
          </>
        );
      },
    },
  ];

  return (
    <div className="stack">
      <header className="page-head">
        <h2 className="page-head-title">Workspaces</h2>
        <p className="page-head-sub">
          {num(all.length)} total · {num(abandoned.length)} with no activity in{" "}
          {derived.windowDays} days
        </p>
      </header>

      <div className="scope-row">
        {SCOPES.map((s) => (
          <button
            key={s.id}
            className={scope === s.id ? "scope active" : "scope"}
            onClick={() => setScope(s.id)}
          >
            {s.label}
          </button>
        ))}
      </div>

      <div className="kpi-row">
        <StatTile label="Workspaces" value={num(all.length)} />
        <StatTile
          label="Fabric workspaces"
          value={num(all.filter((s) => s.ws.kind === "Fabric").length)}
        />
        <StatTile
          label="No activity"
          value={num(abandoned.length)}
          tone={abandoned.length > 0 ? "warning" : "good"}
          caption={`in ${derived.windowDays} days`}
        />
        <StatTile
          label="No admin"
          value={num(ownerless.length)}
          tone={ownerless.length > 0 ? "critical" : "good"}
          caption="nobody can grant access"
        />
      </div>

      <FilterBar
        filters={[
          {
            key: "ws",
            label: "Workspace name",
            options: wsNames,
            value: wsFilter,
            onChange: setWsFilter,
          },
          {
            key: "kind",
            label: "Workspace type",
            options: ["Fabric", "Premium", "PPU", "Pro"],
            value: kind,
            onChange: setKind,
          },
          {
            key: "own",
            label: "Ownership",
            options: ["No admin", "Sole admin"],
            value: ownership,
            onChange: setOwnership,
          },
        ]}
      />

      <Card
        title={`${num(rows.length)} of ${num(all.length)} workspaces`}
        subtitle="Click a workspace to see its models, reports and members."
      >
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(s) => s.ws.id || s.ws.name}
          onRowClick={onOpen}
          initialSort="activity"
          initialDir="asc"
        />
      </Card>
    </div>
  );
}
