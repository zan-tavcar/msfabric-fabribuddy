import { useCallback, useMemo, useState } from "react";
import type { Report, SemanticModel, TenantData } from "../api/types";
import type { Derived } from "../lib/insights";
import { Card, StatTile } from "../components/widgets";
import { DataTable, FilterBar, StatusPill, type Column } from "../components/DataTable";
import { activityPill } from "../lib/pills";
import { num } from "../lib/format";

interface ModelRow {
  model: SemanticModel;
  reports: Report[];
  lastViewed: string | null;
}

/**
 * One hierarchical table: a semantic model expands to the reports built on it.
 *
 * Reports are where usage lives, so "last viewed" sits on the report row; a
 * model's own figure is simply the most recent view across its reports.
 */
export function ModelsAndReports({
  data,
  derived,
  onOpenReport,
}: {
  data: TenantData;
  derived: Derived;
  onOpenReport: (reportId: string) => void;
}) {
  const [ws, setWs] = useState<string[]>([]);
  const [usage, setUsage] = useState<string[]>([]);

  // Any interaction, not just a view — a refresh or an edit is activity too.
  const lastViewOf = useCallback(
    (name: string) =>
      derived.lastSeenByItem.get(name.toLowerCase()) ??
      derived.lastSeenByReport.get(name.toLowerCase()) ??
      null,
    [derived.lastSeenByItem, derived.lastSeenByReport],
  );

  const rows: ModelRow[] = useMemo(() => {
    const byModel = new Map<string, Report[]>();
    for (const r of data.reports) {
      const k = (r.model || "").toLowerCase();
      if (!byModel.has(k)) byModel.set(k, []);
      byModel.get(k)!.push(r);
    }
    const out = data.models.map((m) => {
      const reports = byModel.get(m.name.toLowerCase()) ?? [];
      const stamps = reports
        .map((r) => lastViewOf(r.name))
        .filter((x): x is string => Boolean(x));
      stamps.sort();
      return { model: m, reports, lastViewed: stamps.at(-1) ?? null };
    });

    // Reports whose model was not in the scan still deserve a home.
    const orphaned = data.reports.filter(
      (r) => !r.model || !data.models.some((m) => m.name === r.model),
    );
    if (orphaned.length) {
      const stamps = orphaned
        .map((r) => lastViewOf(r.name))
        .filter((x): x is string => Boolean(x));
      stamps.sort();
      out.push({
        model: {
          id: "__none__", name: "(no semantic model)", ws: "—", wsType: "",
          configuredBy: "", isRefreshable: null,
        },
        reports: orphaned,
        lastViewed: stamps.at(-1) ?? null,
      });
    }
    return out;
  }, [data.models, data.reports, lastViewOf]);

  const filtered = rows.filter((r) => {
    if (ws.length && !ws.includes(r.model.ws)) return false;
    const usageOf = (u: string) =>
      u === "Never viewed"
        ? !r.lastViewed
        : u === "Viewed"
          ? Boolean(r.lastViewed)
          : u === "No reports"
            ? r.reports.length === 0
            : true;
    if (usage.length && !usage.some(usageOf)) return false;
    return true;
  });

  const wsNames = useMemo(
    () => [...new Set(data.models.map((m) => m.ws))].sort(),
    [data.models],
  );

  const neverViewed = rows.filter((r) => !r.lastViewed).length;
  const noReports = rows.filter((r) => r.reports.length === 0).length;

  const columns: Column<ModelRow>[] = [
    {
      key: "name",
      label: "Semantic model",
      value: (r) => r.model.name.toLowerCase(),
      render: (r) => <span className="strong">{r.model.name}</span>,
    },
    {
      key: "ws",
      label: "Workspace",
      width: "240px",
      value: (r) => r.model.ws.toLowerCase(),
      render: (r) => <span className="muted-note">{r.model.ws}</span>,
    },
    {
      key: "owner",
      label: "Owner",
      width: "250px",
      value: (r) => (r.model.configuredBy || "").toLowerCase(),
      render: (r) => <span className="muted-note">{r.model.configuredBy || "—"}</span>,
    },
    {
      key: "reports",
      label: "Reports",
      width: "100px",
      value: (r) => r.reports.length,
      render: (r) => (
        <span className={r.reports.length === 0 ? "tnum muted-note" : "tnum"}>
          {num(r.reports.length)}
        </span>
      ),
    },
    {
      key: "viewed",
      label: "Last viewed",
      width: "210px",
      value: (r) => activityPill(r.lastViewed).sort,
      render: (r) => {
        const p = activityPill(r.lastViewed);
        return (
          <>
            <StatusPill tone={p.tone}>{p.label}</StatusPill>
            {r.lastViewed && (
              <div className="muted-note">{new Date(r.lastViewed).toLocaleString()}</div>
            )}
          </>
        );
      },
    },
  ];

  return (
    <div className="stack">
      <header className="page-head">
        <h2 className="page-head-title">Models &amp; reports</h2>
        <p className="page-head-sub">
          {num(data.models.length)} semantic models · {num(data.reports.length)} reports ·{" "}
          {num(neverViewed)} models with no views in {derived.windowDays} days
        </p>
      </header>

      <div className="kpi-row">
        <StatTile label="Semantic models" value={num(data.models.length)} />
        <StatTile label="Reports" value={num(data.reports.length)} />
        <StatTile
          label="Never viewed"
          value={num(neverViewed)}
          tone={neverViewed > 0 ? "warning" : "good"}
          caption={`no views in ${derived.windowDays} days`}
        />
        <StatTile
          label="Models with no reports"
          value={num(noReports)}
          tone={noReports > 0 ? "warning" : "good"}
        />
      </div>

      <FilterBar
        filters={[
          { key: "ws", label: "Workspace", options: wsNames, value: ws, onChange: setWs },
          {
            key: "usage",
            label: "Usage",
            options: ["Viewed", "Never viewed", "No reports"],
            value: usage,
            onChange: setUsage,
          },
        ]}
      />

      <Card
        title={`${num(filtered.length)} of ${num(rows.length)} semantic models`}
        subtitle="Expand a model to see its reports. Click a report to see exactly who can view it."
      >
        <DataTable
          columns={columns}
          rows={filtered}
          rowKey={(r) => r.model.id || r.model.name}
          initialSort="viewed"
          initialDir="asc"
          canExpand={(r) => r.reports.length > 0}
          expand={(r) => (
            <div className="nested">
              <div className="nested-title">
                {num(r.reports.length)} report{r.reports.length === 1 ? "" : "s"} on this model
              </div>
              <table className="data-table dense nested-table">
                <tbody>
                  {r.reports.map((rep) => {
                    const p = activityPill(lastViewOf(rep.name));
                    return (
                      <tr
                        key={rep.id || rep.name}
                        className="clickable-row"
                        onClick={() => onOpenReport(rep.id)}
                      >
                        <td>
                          <span className="strong link-like">{rep.name}</span>
                        </td>
                        <td style={{ width: 240 }} className="muted-note">
                          {rep.ws}
                        </td>
                        <td style={{ width: 230 }}>
                          <StatusPill tone={p.tone}>{p.label}</StatusPill>
                          {lastViewOf(rep.name) && (
                            <span className="muted-note">
                              {" "}
                              {new Date(lastViewOf(rep.name)!).toLocaleString()}
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        />
      </Card>
    </div>
  );
}
