import { Layers } from "lucide-react";
import type { TenantData } from "../api/types";
import type { Derived, ViewId } from "../lib/insights";
import { Card, StatTile, UnavailableNote } from "../components/widgets";
import { FindingCard } from "../components/FindingCard";
import { LineChart } from "../charts/LineChart";
import { BarList } from "../charts/BarList";
import { Meter } from "../charts/Meter";
import { num, pct } from "../lib/format";

export function Overview({
  data,
  derived,
  onNavigate,
}: {
  data: TenantData;
  derived: Derived;
  onNavigate: (v: ViewId) => void;
}) {
  const { summary, activity } = data;
  const { findings, windowDays } = derived;

  // KPIs lead; every finding sits with the others below (no hero banner).
  const rest = findings;

  const viewedReports = data.reports.length - derived.dormantReports.length;
  const peakUsers = Math.max(0, ...derived.daily.map((d) => d.viewers));

  return (
    <div className="stack">
      <div className="kpi-row">
        <StatTile label="Workspaces" value={num(summary.workspaces)} />
        <StatTile label="Reports" value={num(summary.reports)} />
        <StatTile label="Semantic models" value={num(summary.models)} />
        <StatTile
          label="Licences assigned"
          value={
            data.licenses.live
              ? `${num(data.licenses.summary.pro)} Pro · ${num(data.licenses.summary.ppu)} PPU`
              : "—"
          }
          caption={
            data.licenses.live
              ? `${num(data.licenses.summary.unused)} unused in ${windowDays} days`
              : "licences unavailable"
          }
        />
        <StatTile
          label="Unique Power BI users"
          value={num(activity.summary.distinctUsers)}
          caption={`${num(activity.summary.viewerUsers ?? 0)} opened a report · peak ${num(peakUsers)}/day`}
        />
      </div>

      <div className="two-col">
        <Card
          title="What people actually open"
          subtitle={`Most-viewed reports in the last ${windowDays} days.`}
        >
          <BarList
            data={activity.topReports.slice(0, 8).map((r) => ({
              label: r.report,
              value: r.views,
            }))}
            emptyText="No report views recorded in this window."
          />
        </Card>

        <Card
          title="How much of the estate is alive"
          subtitle={`Reports opened at least once in the last ${windowDays} days.`}
        >
          {derived.activityKnown && data.reports.length > 0 ? (
            <>
              <Meter
                value={viewedReports}
                total={data.reports.length}
                tone={pct(viewedReports, data.reports.length) < 30 ? "warning" : "good"}
                label="Reports opened at least once"
                caption="Dormant reports still hold data and still expose it. They are the cheapest thing to retire."
              />
              <div style={{ marginTop: 18 }}>
                <Meter
                  value={derived.orphanModels.length}
                  total={data.models.length}
                  tone={derived.orphanModels.length > 0 ? "warning" : "good"}
                  label="Semantic models with no reports built on them"
                  caption="If these still refresh on a schedule, they burn capacity for nothing."
                />
              </div>
            </>
          ) : (
            <UnavailableNote reason={activity.error ?? "No activity data to compare against."} />
          )}
        </Card>
      </div>

      <Card
        title="Is anyone actually using Power BI?"
        subtitle="Report views per day, and how many distinct people were behind them."
      >
        {activity.live ? (
          <>
            <LineChart
              series={[
                {
                  key: "views",
                  label: "Report views",
                  color: "var(--series-1)",
                  data: derived.daily.map((d) => ({ date: d.date, value: d.views })),
                },
                {
                  key: "viewers",
                  label: "People who opened a report",
                  color: "var(--series-2)",
                  data: derived.daily.map((d) => ({ date: d.date, value: d.viewers })),
                },
              ]}
              height={240}
            />
            <div className="chart-foot">
              <span>
                {num(activity.summary.humanUsers ?? 0)} people ·{" "}
                {num(activity.summary.humanEvents ?? 0)} human actions of{" "}
                {num(activity.summary.total)} total events
              </span>
              {activity.summary.partial && (
                <span className="muted-note">
                  Covers {activity.summary.days} of{" "}
                  {activity.summary.daysRequested ?? 30} days — the pull stopped early.
                </span>
              )}
            </div>
          </>
        ) : (
          <UnavailableNote reason={activity.error ?? "No activity data."} />
        )}
      </Card>

      {rest.length > 0 && (
        <section>
          <header className="section-head">
            <h2>
              <Layers size={16} strokeWidth={2.2} />
              Everything else worth knowing
            </h2>
            <p>Ordered by how much it should bother you.</p>
          </header>
          <div className="findings">
            {rest.map((f) => (
              <FindingCard key={f.id} finding={f} onNavigate={onNavigate} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
