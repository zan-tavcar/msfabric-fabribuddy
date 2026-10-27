import { useMemo, useState } from "react";
import type { ActivityEvent, TenantData } from "../api/types";
import type { Derived } from "../lib/insights";
import { Card, StatTile, UnavailableNote } from "../components/widgets";
import { DataTable, FilterBar, StatusPill, type Column } from "../components/DataTable";
import { LineChart } from "../charts/LineChart";
import { BarList } from "../charts/BarList";
import { activityPill } from "../lib/pills";
import { num } from "../lib/format";

/**
 * Usage, built entirely from report views.
 *
 * The filter row sits above everything and scopes ALL of it — KPIs, chart,
 * both bar lists and the log. Filters that narrowed only the table underneath
 * were worse than none: the headline figures kept contradicting the rows.
 */
export function ActivityView({
  data,
  derived,
}: {
  data: TenantData;
  derived: Derived;
}) {
  // Options come from the whole tenant, not just the rows with views: you
  // often want to pick a workspace precisely to discover it has none.
  const [ws, setWs] = useState<string[]>([]);
  const [user, setUser] = useState<string[]>([]);
  const [item, setItem] = useState<string[]>([]);
  const { activity } = data;

  const views = useMemo(
    () => activity.events.filter((e) => e.action === "ViewReport"),
    [activity.events],
  );

  const filtered = useMemo(
    () =>
      views.filter((e) => {
        if (ws.length && !ws.includes(e.ws)) return false;
        if (user.length && !user.includes(e.user)) return false;
        if (item.length && !item.includes(e.report)) return false;
        return true;
      }),
    [views, ws, user, item],
  );

  const filtering = Boolean(ws.length || user.length || item.length);

  const opts = useMemo(() => {
    const uniq = (f: (e: ActivityEvent) => string) =>
      new Set(views.map(f).filter(Boolean));
    const merge = (fromEvents: Set<string>, fromScan: string[]) =>
      [...new Set([...fromEvents, ...fromScan.filter(Boolean)])].sort((a, b) =>
        a.localeCompare(b),
      );
    return {
      ws: merge(uniq((e) => e.ws), data.workspaces.map((w) => w.name)),
      users: [...uniq((e) => e.user)].sort(),
      items: merge(uniq((e) => e.report), data.reports.map((r) => r.name)),
    };
  }, [views, data.workspaces, data.reports]);

  /** Everything below comes from the filtered slice, never the whole set. */
  const scoped = useMemo(() => {
    const byDay = new Map<string, { views: number; users: Set<string> }>();
    const byReport = new Map<string, number>();
    const byUser = new Map<string, number>();
    for (const e of filtered) {
      const day = (e.ts || "").slice(0, 10);
      if (day) {
        const b = byDay.get(day) ?? { views: 0, users: new Set<string>() };
        b.views += 1;
        if (e.user) b.users.add(e.user.toLowerCase());
        byDay.set(day, b);
      }
      if (e.report) byReport.set(e.report, (byReport.get(e.report) ?? 0) + 1);
      if (e.user) byUser.set(e.user, (byUser.get(e.user) ?? 0) + 1);
    }
    // Keep every day in the window so a gap reads as silence, not absence.
    const daily = derived.daily.map((d) => ({
      date: d.date,
      views: byDay.get(d.date)?.views ?? 0,
      viewers: byDay.get(d.date)?.users.size ?? 0,
    }));
    return {
      daily,
      topReports: [...byReport.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([report, v]) => ({ label: report, value: v })),
      topUsers: [...byUser.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([u, v]) => ({ label: u, value: v })),
      people: new Set(filtered.map((e) => (e.user || "").toLowerCase()).filter(Boolean)).size,
      reports: byReport.size,
      peak: Math.max(0, ...daily.map((d) => d.views)),
    };
  }, [filtered, derived.daily]);

  if (!activity.live) {
    return (
      <Card title="Usage and activity">
        <UnavailableNote reason={activity.error ?? "No activity data."} />
        <p className="muted-note" style={{ marginTop: 12 }}>
          {activity.running
            ? "A scan is in flight. It reads the audit log one day at a time, so a 30-day window takes several minutes."
            : "Use Scan → Usage activity in the header. It runs in the background, so you can keep working while it finishes."}
        </p>
      </Card>
    );
  }

  const columns: Column<ActivityEvent>[] = [
    {
      key: "ts",
      label: "When",
      width: "210px",
      value: (e) => e.ts,
      render: (e) => (
        <>
          <StatusPill tone={activityPill(e.ts).tone}>{activityPill(e.ts).label}</StatusPill>
          <div className="muted-note">{e.ts ? new Date(e.ts).toLocaleString() : "—"}</div>
        </>
      ),
    },
    {
      key: "user",
      label: "Who",
      width: "280px",
      value: (e) => e.user.toLowerCase(),
      render: (e) => e.user || <span className="muted-note">unknown</span>,
    },
    {
      key: "item",
      label: "Report",
      value: (e) => e.report.toLowerCase(),
      render: (e) => <span className="strong">{e.report || "—"}</span>,
    },
    {
      key: "ws",
      label: "Workspace",
      width: "230px",
      value: (e) => e.ws.toLowerCase(),
      render: (e) => <span className="muted-note">{e.ws || "—"}</span>,
    },
  ];

  return (
    <div className="stack">
      <header className="page-head">
        <h2 className="page-head-title">Usage &amp; activity</h2>
        <p className="page-head-sub">
          Report views across the last {derived.windowDays} days
          {activity.scope?.scoped
            ? ` in the ${activity.scope.workspaces.length} scanned workspace${activity.scope.workspaces.length === 1 ? "" : "s"}`
            : " across the tenant"}
          {activity.summary.partial &&
            ` · window stopped at ${activity.summary.days} of ${activity.summary.daysRequested ?? 30} days`}
        </p>
        {activity.scope?.scoped && activity.scope.viewsElsewhere > 0 && (
          <p className="muted-note">
            {num(activity.scope.viewsElsewhere)} views in workspaces you have not selected are
            not shown here. They still count when deciding whether a seat is used.
          </p>
        )}
      </header>

      <FilterBar
        filters={[
          { key: "ws", label: "Workspace", options: opts.ws, value: ws, onChange: setWs },
          { key: "user", label: "User", options: opts.users, value: user, onChange: setUser },
          { key: "item", label: "Report", options: opts.items, value: item, onChange: setItem },
        ]}
      />

      {filtering && filtered.length === 0 && (
        <div className="scope-note">
          No report views match this filter. The workspace or report exists, but
          nobody opened it in the last {derived.windowDays} days.
        </div>
      )}

      {filtering && filtered.length > 0 && (
        <div className="scope-note">
          Showing {num(filtered.length)} of {num(views.length)} report views — every
          figure and chart below reflects this filter.
        </div>
      )}

      <div className="kpi-row">
        <StatTile
          label="Report views"
          value={num(filtered.length)}
          caption={filtering ? `of ${num(views.length)} total` : `last ${derived.windowDays} days`}
        />
        <StatTile label="People who opened a report" value={num(scoped.people)} />
        <StatTile label="Reports opened" value={num(scoped.reports)} />
        <StatTile label="Busiest day" value={num(scoped.peak)} caption="views in one day" />
      </div>

      <Card
        title="Report views over time"
        subtitle="Views per day and the distinct people behind them, on one shared scale."
      >
        <LineChart
          series={[
            {
              key: "views",
              label: "Report views",
              color: "var(--series-1)",
              data: scoped.daily.map((d) => ({ date: d.date, value: d.views })),
            },
            {
              key: "viewers",
              label: "People who opened a report",
              color: "var(--series-2)",
              data: scoped.daily.map((d) => ({ date: d.date, value: d.viewers })),
            },
          ]}
          height={260}
        />
      </Card>

      <div className="two-col">
        <Card title="Most-viewed reports">
          <BarList
            data={scoped.topReports.slice(0, 10)}
            emptyText="No views match these filters."
          />
        </Card>
        <Card title="Most active people">
          <BarList
            data={scoped.topUsers.slice(0, 10)}
            color="var(--series-2)"
            emptyText="No views match these filters."
          />
        </Card>
      </div>

      <Card title="Report views" subtitle={`${num(filtered.length)} views, newest first.`}>
        <DataTable
          dense
          columns={columns}
          rows={filtered}
          rowKey={(e) => `${e.ts}-${e.user}-${e.report}`}
          initialSort="ts"
          initialDir="desc"
          empty="No report views match those filters."
        />
      </Card>
    </div>
  );
}
