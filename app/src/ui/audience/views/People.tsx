import { useMemo, useState } from "react";
import type { Derived } from "../lib/insights";
import type { AccessIndex } from "../lib/access";
import { Avatar, Card, StatTile, UnavailableNote } from "../components/widgets";
import { DataTable, FilterBar, StatusPill, type Column } from "../components/DataTable";
import { initials, num, relativeTime } from "../lib/format";

interface Row {
  key: string;
  graphId: string;
  name: string;
  upn: string;
  isGroup: boolean;
  workspaces: number;
  reports: number;
  models: number;
  viaGroup: boolean;
  lastSeen: string | null;
  license: string | null;
  unusedSeat: boolean;
}

/**
 * People and groups, with Entra group membership resolved.
 *
 * A principal here is either a named person or an Entra security group. When a
 * person only reaches content because they are inside a group, that is stated
 * — otherwise a governance review sees "3 principals" where the reality is
 * "everyone in All Employees".
 */
export function People({
  derived,
  access,
  onOpenPerson,
}: {
  derived: Derived;
  access: AccessIndex | null;
  onOpenPerson: (key: string) => void;
}) {
  const [kind, setKind] = useState<string[]>([]);
  const [via, setVia] = useState<string[]>([]);

  const rows: Row[] = useMemo(() => {
    // Prefer the permission scan: it knows group membership. Fall back to the
    // plain admin scan when permissions have never been scanned.
    if (access) {
      const licByName = new Map(
        derived.peopleStats.map((p) => [p.name.toLowerCase(), p]),
      );
      return access.principalList.map((p) => {
        const lic = licByName.get(p.principal.name.toLowerCase());
        const models = new Set(p.reports.map((r) => r.modelName).filter(Boolean));
        return {
          key: p.principal.upn || p.principal.graphId || p.principal.name,
          graphId: p.principal.graphId,
          name: p.principal.name,
          upn: p.principal.upn,
          isGroup: p.principal.type === "Group",
          workspaces: p.workspaces.length,
          reports: p.reports.length,
          models: models.size,
          viaGroup: p.reports.some(
            (r) => r.reportGrant?.viaGroup || r.modelGrant?.viaGroup,
          ),
          lastSeen: lic?.lastSeen ?? null,
          license: lic?.license ?? null,
          unusedSeat: Boolean(lic?.unusedSeat),
        };
      });
    }
    return derived.peopleStats.map((p) => ({
      key: p.name,
      graphId: "",
      name: p.name,
      upn: "",
      isGroup: p.principalType === "Group",
      workspaces: p.workspaces,
      reports: p.reachableReports,
      models: 0,
      viaGroup: false,
      lastSeen: p.lastSeen,
      license: p.license,
      unusedSeat: p.unusedSeat,
    }));
  }, [access, derived.peopleStats]);

  const filtered = rows.filter((r) => {
    const isKind = (k: string) =>
      k === "People" ? !r.isGroup : k === "Entra groups" ? r.isGroup : true;
    if (kind.length && !kind.some(isKind)) return false;
    const isVia = (v: string) =>
      v === "Only via group" ? Boolean(r.viaGroup) : v === "Directly granted" ? !r.viaGroup : true;
    if (via.length && !via.some(isVia)) return false;
    return true;
  });

  const members = (r: Row) => (access?.groupMembers?.[r.graphId] ?? []);

  const groups = rows.filter((r) => r.isGroup).length;
  const viaGroups = rows.filter((r) => r.viaGroup).length;

  const columns: Column<Row>[] = [
    {
      key: "name",
      label: "Principal",
      value: (r) => r.name.toLowerCase(),
      render: (r) => (
        <div className="person">
          <Avatar initials={initials(r.name)} />
          <div>
            <span className="strong link-like">{r.name}</span>
            {r.upn && <div className="muted-note">{r.upn}</div>}
          </div>
        </div>
      ),
    },
    {
      key: "type",
      label: "Type",
      width: "130px",
      value: (r) => (r.isGroup ? "Group" : "User"),
      render: (r) =>
        r.isGroup ? (
          <StatusPill tone="warning">Entra group</StatusPill>
        ) : (
          <StatusPill tone="neutral">User</StatusPill>
        ),
    },
    {
      key: "how",
      label: "Access via",
      width: "150px",
      value: (r) => (r.viaGroup ? 1 : 0),
      render: (r) =>
        r.viaGroup ? (
          <StatusPill tone="neutral">Group membership</StatusPill>
        ) : (
          <span className="muted-note">Direct</span>
        ),
    },
    {
      key: "ws",
      label: "Workspaces",
      width: "120px",
      value: (r) => r.workspaces,
      render: (r) => <span className="tnum">{num(r.workspaces)}</span>,
    },
    {
      key: "reports",
      label: "Reports in reach",
      width: "150px",
      value: (r) => r.reports,
      render: (r) => <span className="tnum">{num(r.reports)}</span>,
    },
    {
      key: "models",
      label: "Semantic models in reach",
      width: "180px",
      value: (r) => r.models,
      render: (r) => <span className="tnum">{num(r.models)}</span>,
    },
    {
      key: "seen",
      label: "Last seen",
      width: "140px",
      value: (r) => r.lastSeen ?? "",
      render: (r) => <span className="muted-note">{relativeTime(r.lastSeen)}</span>,
    },
    {
      key: "lic",
      label: "Licence",
      width: "150px",
      value: (r) => r.license ?? "",
      render: (r) => (
        <div className="tag-row">
          {r.license ? (
            <StatusPill tone={r.license === "PPU" ? "good" : "neutral"}>{r.license}</StatusPill>
          ) : (
            <span className="muted-note">—</span>
          )}
          {r.unusedSeat && <StatusPill tone="critical">Unused</StatusPill>}
        </div>
      ),
    },
  ];

  return (
    <div className="stack">
      <header className="page-head">
        <h2 className="page-head-title">People</h2>
        <p className="page-head-sub">
          {num(rows.length)} principals · {num(groups)} Entra groups ·{" "}
          {num(viaGroups)} reach content only through a group
        </p>
      </header>

      {!access && (
        <UnavailableNote reason="Entra group membership is resolved by the permission scan. Run it from 'Who sees what' to see which named people are inside each group." />
      )}

      <div className="kpi-row">
        <StatTile label="Principals" value={num(rows.length)} />
        <StatTile
          label="Entra groups"
          value={num(groups)}
          caption={access ? `${num(access.stats.groupsExpanded)} expanded` : "not expanded yet"}
        />
        <StatTile
          label="Reach content via a group"
          value={num(viaGroups)}
          tone={undefined}
          caption="not granted by name"
        />
        <StatTile
          label="Paid but silent"
          value={num(rows.filter((r) => r.unusedSeat).length)}
          tone={rows.some((r) => r.unusedSeat) ? "critical" : "good"}
        />
      </div>

      <FilterBar
        filters={[
          {
            key: "kind",
            label: "Principal type",
            options: ["People", "Entra groups"],
            value: kind,
            onChange: setKind,
          },
          {
            key: "via",
            label: "How they got access",
            options: ["Only via group", "Directly granted"],
            value: via,
            onChange: setVia,
          },
        ]}
      />

      <Card
        title={`${num(filtered.length)} of ${num(rows.length)} principals`}
        subtitle="Click a principal to see exactly which reports they can open and why."
      >
        <DataTable
          columns={columns}
          rows={filtered}
          rowKey={(r) => r.key}
          onRowClick={(r) => onOpenPerson(r.key)}
          initialSort="reports"
          initialDir="desc"
          canExpand={(r) => r.isGroup && (members(r).length > 0)}
          expand={(r) => (
            <div className="nested">
              <div className="nested-title">
                {num(members(r).length)} people in {r.name}
              </div>
              <ul className="nested-list">
                {members(r).map((m) => (
                  <li key={m.id || m.upn || m.name}>
                    <Avatar initials={initials(m.name)} />
                    <span>
                      {m.name}
                      {m.upn && <span className="muted-note"> · {m.upn}</span>}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        />
      </Card>
    </div>
  );
}
