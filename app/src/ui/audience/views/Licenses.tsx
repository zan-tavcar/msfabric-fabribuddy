import { useState } from "react";
import type { TenantData } from "../api/types";
import type { Derived } from "../lib/insights";
import { Card, StatTile, UnavailableNote } from "../components/widgets";
import { DataTable, StatusPill, type Column } from "../components/DataTable";
import { activityPill } from "../lib/pills";
import type { LicenseRow } from "../api/types";
import { Meter } from "../charts/Meter";
import { num, pct } from "../lib/format";

export function Licenses({
  data,
  derived,
}: {
  data: TenantData;
  derived: Derived;
}) {
  const { licenses } = data;
  const [onlyUnused, setOnlyUnused] = useState(false);
  const [q, setQ] = useState("");

  if (!licenses.live) {
    return (
      <Card
        title="Licences"
        subtitle="Pro and PPU seats, cross-referenced with activity."
      >
        <UnavailableNote reason={licenses.error ?? "Licence data is unavailable."} />
        <p className="muted-note" style={{ marginTop: 12 }}>
          This needs Microsoft Graph application permissions on the service
          principal — <code>User.Read.All</code> and{" "}
          <code>Organization.Read.All</code>, both admin-consented. Everything
          else in FabriBuddy works without them.
        </p>
      </Card>
    );
  }

  const s = licenses.summary;
  const rows = licenses.rows
    .filter((r) => (onlyUnused ? r.unused : true))
    .filter((r) => r.name.toLowerCase().includes(q.toLowerCase()))
    .sort((a, b) => Number(b.unused) - Number(a.unused) || a.name.localeCompare(b.name));

  const columns: Column<LicenseRow>[] = [
    {
      key: "name",
      label: "Person",
      value: (r) => r.name.toLowerCase(),
      render: (r) => (
        <>
          <span className="strong">{r.name}</span>
          {r.upn && <div className="muted-note">{r.upn}</div>}
        </>
      ),
    },
    {
      key: "license",
      label: "Licence",
      width: "120px",
      value: (r) => r.license,
      render: (r) => (
        <StatusPill tone={r.license === "PPU" ? "good" : "neutral"}>{r.license}</StatusPill>
      ),
    },
    {
      key: "seen",
      label: "Last activity",
      width: "220px",
      value: (r) => activityPill(r.lastSeen ?? null).sort,
      render: (r) => {
        const p = activityPill(r.lastSeen ?? null);
        return (
          <>
            <StatusPill tone={p.tone}>{p.label}</StatusPill>
            {r.lastSeen && (
              <div className="muted-note">{new Date(r.lastSeen).toLocaleString()}</div>
            )}
          </>
        );
      },
    },
    {
      key: "status",
      label: "Status",
      width: "180px",
      value: (r) => (r.unused ? 1 : 0),
      render: (r) =>
        !r.activityKnown ? (
          <span className="muted-note">No activity data</span>
        ) : r.unused ? (
          <StatusPill tone="critical">No activity in window</StatusPill>
        ) : (
          <StatusPill tone="good">Active</StatusPill>
        ),
    },
  ];

  return (
    <div className="stack">
      <div className="kpi-row">
        <StatTile label="Paid seats" value={num(s.total)} caption="Pro and PPU" />
        <StatTile label="Pro" value={num(s.pro)} />
        <StatTile label="PPU" value={num(s.ppu)} />
        <StatTile
          label="Unused"
          value={num(s.unused)}
          tone={s.unused > 0 ? "critical" : "good"}
          caption={
            s.activityJoined
              ? `${pct(s.unused, s.total)}% of all seats`
              : "needs activity data"
          }
        />
      </div>

      <Card
        title="Seats you could reclaim"
        subtitle={`A seat counts as unused when its holder recorded no Power BI activity in the last ${derived.windowDays} days.`}
      >
        <Meter
          value={s.unused}
          total={s.total}
          tone={s.unused === 0 ? "good" : s.unused / Math.max(s.total, 1) > 0.2 ? "critical" : "warning"}
          label="Unused of total paid seats"
          caption={
            s.activityJoined
              ? "Confirm against a longer window before removing anyone — a 30-day silence can just be a long holiday."
              : "No activity data was available, so nothing is flagged. Nobody is called unused without evidence."
          }
        />
      </Card>

      <Card
        title="Every paid seat"
        subtitle={`${num(rows.length)} shown`}
        right={
          <div className="toolbar">
            <input
              className="input"
              placeholder="Filter by name…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <label className="toggle">
              <input
                type="checkbox"
                checked={onlyUnused}
                onChange={(e) => setOnlyUnused(e.target.checked)}
              />
              Unused only
            </label>
          </div>
        }
      >
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r) => `${r.name}-${r.license}`}
          initialSort="seen"
          initialDir="asc"
        />
      </Card>
    </div>
  );
}
