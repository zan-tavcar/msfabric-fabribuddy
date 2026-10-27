import { useEffect, useMemo, useState } from "react";
import {
  Eye,
  Globe,
  Link2,
  Loader2,
  Search,
  ShieldAlert,
  Users2,
} from "lucide-react";
import { audienceApi } from "../api/client";
import type { PermissionScan } from "../api/types";
import {
  buildAccessIndex,
  sourceLabel,
  type AccessIndex,
  type Grant,
  type ReportAccess,
} from "../lib/access";
import { Card, StatTile, Tag, UnavailableNote } from "../components/widgets";
import { DataTable, type Column } from "../components/DataTable";
import { initials, num } from "../lib/format";
import { Avatar } from "../components/widgets";

type Mode = "report" | "person";
/** Which access type the report's table shows. */
type TypeFilter = "all" | "workspace_role" | "direct_access";

/**
 * How one half of the access was obtained, as plain text.
 *
 * Chips and buttons here were pure noise: with two path columns per row the
 * table became unreadable. A sentence carries the same information and scans
 * far faster.
 */
function PathText({ grant }: { grant: Grant | null }) {
  if (!grant) return <span className="path-text none">No access</span>;
  return (
    <span className="path-text">
      {grant.source === "direct_access"
        ? "Direct access on "
        : "Workspace role on "}
      <span className="via">{grant.via}</span>
      {" · "}
      {grant.right}
      {grant.viaGroup && (
        <>
          {" · "}
          <span className="grp">via group {grant.viaGroup}</span>
        </>
      )}
    </span>
  );
}

export function Access({
  perm,
  scanning,
  onScan,
  focusReport,
  focusPerson,
  onFocusHandled,
}: {
  perm: PermissionScan | null;
  scanning: boolean;
  onScan: () => void;
  /** A report to open directly, e.g. when arriving from a workspace. */
  focusReport?: string | null;
  /** A principal to open directly, e.g. when arriving from People. */
  focusPerson?: string | null;
  onFocusHandled?: () => void;
}) {
  const [mode, setMode] = useState<Mode>("report");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [appliedFocus, setAppliedFocus] = useState<string | null>(null);

  // Arriving from another page carries a report or person to open. Deriving it
  // during render is React's documented way to adapt state to a prop change —
  // doing it in an effect causes a visible second render where the pane is
  // still empty.
  const focus = focusReport ?? focusPerson ?? null;
  if (focus && focus !== appliedFocus) {
    setAppliedFocus(focus);
    setSelected(focus);
    setMode(focusReport ? "report" : "person");
    setQ("");
  }

  useEffect(() => {
    if (focus) onFocusHandled?.();
  }, [focus, onFocusHandled]);

  const index = useMemo(() => (perm ? buildAccessIndex(perm) : null), [perm]);

  if (!perm || !index) {
    return (
      <Card
        title="Who can see what"
        subtitle="Effective access, resolved across workspace roles and direct shares."
      >
        <div className="scan-prompt">
          <ShieldAlert size={28} strokeWidth={1.8} />
          <h3>Permissions have not been scanned yet</h3>
          <p>
            This reads per-item grants through the Power BI admin scanner API —
            one asynchronous round trip per 100 workspaces. On a large tenant it
            takes a few minutes, so it is a deliberate action rather than
            something that runs on every page load.
          </p>
          <button
            className="btn btn-primary btn-lg"
            onClick={onScan}
            disabled={scanning}
          >
            {scanning ? (
              <>
                <Loader2 size={16} className="spin" /> Scanning permissions…
              </>
            ) : (
              <>
                <Search size={16} strokeWidth={2.2} /> Scan permissions
              </>
            )}
          </button>
        </div>
      </Card>
    );
  }

  const s = index.stats;
  const selectedReport =
    mode === "report" && selected ? index.byReport.get(selected) : null;
  const selectedPerson =
    mode === "person" && selected ? index.byPrincipal.get(selected) : null;

  const reports = index.reportList.filter((r) =>
    `${r.reportName} ${r.wsName}`.toLowerCase().includes(q.toLowerCase()),
  );
  const people = index.principalList.filter((p) =>
    `${p.principal.name} ${p.principal.upn}`
      .toLowerCase()
      .includes(q.toLowerCase()),
  );

  return (
    <div className="stack">
      <div className="kpi-row">
        <StatTile label="Reports resolved" value={num(s.reports)} />
        <StatTile
          label="Principals"
          value={num(s.principals)}
          caption={`${num(s.groups)} are groups`}
        />
        <StatTile
          label="Entra groups expanded"
          value={num(s.groupsExpanded)}
          caption={`${num(s.peopleViaGroups)} people reached only via a group`}
        />
        <StatTile
          label="Nobody can view"
          value={num(s.reportsNobodyCanView)}
          tone={s.reportsNobodyCanView > 0 ? "warning" : "good"}
          caption="report or model access missing"
        />
        <StatTile
          label="Shared by link"
          value={num(s.orgLinks)}
          tone={s.publishedToWeb > 0 ? "warning" : undefined}
          caption={
            s.publishedToWeb > 0
              ? `${num(s.publishedToWeb)} published to web — public`
              : "links for everyone in the organisation"
          }
        />
      </div>

      {perm.stats.widelySharedUnavailable && (
        <UnavailableNote
          reason={`Microsoft's lists of organisation-wide links and Publish-to-web reports could not be read (${perm.stats.widelySharedUnavailable}). Links the scan itself marks are still shown; who created them, and Publish to web, are not.`}
        />
      )}

      {Object.keys(perm.groupErrors ?? {}).length > 0 && (
        <UnavailableNote
          reason={`${Object.keys(perm.groupErrors ?? {}).length} Entra group(s) could not be read from Microsoft Graph, so the people inside them are not resolved. They are shown as groups rather than counted as one person.`}
        />
      )}

      <Card
        title="Who can see what"
        subtitle="A person sees a report only with read on BOTH the report and its semantic model. Access comes from a workspace role, direct access (including “Specific people” links), or a link for everyone in the organisation — and may reach people through an Entra group."
        right={
          <div className="toolbar">
            <div className="segmented">
              <button
                className={mode === "report" ? "seg active" : "seg"}
                onClick={() => {
                  setMode("report");
                  setSelected(null);
                }}
              >
                By report
              </button>
              <button
                className={mode === "person" ? "seg active" : "seg"}
                onClick={() => {
                  setMode("person");
                  setSelected(null);
                }}
              >
                By person
              </button>
            </div>
            <input
              className="input"
              placeholder={
                mode === "report" ? "Find a report…" : "Find a person…"
              }
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
        }
      >
        <div className="master-detail">
          <ul className="master-list">
            {mode === "report"
              ? reports.slice(0, 300).map((r) => (
                  <li key={r.reportId}>
                    <button
                      className={
                        selected === r.reportId ? "ml-item active" : "ml-item"
                      }
                      onClick={() => setSelected(r.reportId)}
                    >
                      <span className="ml-name">
                        {r.reportName}
                        {r.publishedToWeb && (
                          <span
                            className="ml-flag danger"
                            title="Published to web: public on the internet"
                          >
                            <Globe
                              size={12}
                              strokeWidth={2.2}
                              aria-label="Published to web"
                            />
                          </span>
                        )}
                        {r.orgLink && (
                          <span
                            className="ml-flag"
                            title="Shared with a link for everyone in the organisation"
                          >
                            <Link2
                              size={12}
                              strokeWidth={2.2}
                              aria-label="Organisation-wide link"
                            />
                          </span>
                        )}
                      </span>
                      <span className="ml-sub">{r.wsName}</span>
                      <span className="ml-count">
                        <Eye size={12} strokeWidth={2.2} /> {num(r.viewers)}
                      </span>
                    </button>
                  </li>
                ))
              : people.slice(0, 300).map((p) => {
                  const key =
                    p.principal.upn || p.principal.graphId || p.principal.name;
                  return (
                    <li key={key}>
                      <button
                        className={
                          selected === key ? "ml-item active" : "ml-item"
                        }
                        onClick={() => setSelected(key)}
                      >
                        <span className="ml-name">
                          {p.principal.name}
                          {p.principal.type === "Group" && (
                            <Tag tone="neutral">Group</Tag>
                          )}
                        </span>
                        <span className="ml-sub">{p.principal.upn || "—"}</span>
                        <span className="ml-count">
                          <Eye size={12} strokeWidth={2.2} />{" "}
                          {num(p.reports.length)}
                        </span>
                      </button>
                    </li>
                  );
                })}
            {(mode === "report" ? reports : people).length === 0 && (
              <li className="muted-note" style={{ padding: 12 }}>
                Nothing matches that filter.
              </li>
            )}
          </ul>

          <div className="detail-pane">
            {!selected && (
              <div className="detail-empty">
                <Users2 size={26} strokeWidth={1.7} />
                <p>
                  Pick {mode === "report" ? "a report" : "a person"} to see the
                  resolved access paths.
                </p>
              </div>
            )}

            {selectedReport && (
              <ReportDetail access={selectedReport} index={index} />
            )}
            {selectedPerson && (
              <PersonDetail
                person={selectedPerson}
                onOpenReport={(id) => {
                  setMode("report");
                  setSelected(id);
                }}
              />
            )}
          </div>
        </div>
      </Card>
    </div>
  );
}

function ReportDetail({
  access,
  index,
}: {
  access: ReportAccess;
  index: AccessIndex;
}) {
  const [showBlocked, setShowBlocked] = useState(false);
  const [type, setType] = useState<TypeFilter>("all");
  // One row per principal. A person who only appears through a group is folded
  // into that group's expander, so the top level stays readable.
  const rows = access.paths
    .filter((p) => (showBlocked ? true : p.canView))
    .filter((p) => type === "all" || p.reportGrant?.source === type)
    .filter(
      (p) =>
        p.principal.type === "Group" ||
        !(p.reportGrant?.viaGroup || p.modelGrant?.viaGroup),
    );

  const groupMembers = (p: (typeof rows)[number]) =>
    index.groupMembers[p.principal.graphId] ?? [];

  const pathCols: Column<(typeof rows)[number]>[] = [
    {
      key: "who",
      label: "Principal",
      value: (p) => p.principal.name.toLowerCase(),
      render: (p) => (
        <>
          <span className="strong">{p.principal.name}</span>
          {p.principal.type === "Group" && (
            <Tag tone="neutral">Entra group</Tag>
          )}
          {p.principal.upn && (
            <div className="muted-note">{p.principal.upn}</div>
          )}
        </>
      ),
    },
    {
      key: "rep",
      label: "Access to report",
      sortable: false,
      render: (p) => <PathText grant={p.reportGrant} />,
    },
    {
      key: "mod",
      label: "Access to model",
      sortable: false,
      render: (p) => <PathText grant={p.modelGrant} />,
    },
    {
      key: "can",
      label: "Can view",
      width: "110px",
      value: (p) => (p.canView ? 1 : 0),
      render: (p) =>
        p.canView ? (
          <Tag tone="good">Yes</Tag>
        ) : (
          <Tag tone="critical">
            No — {p.blockedBy === "model" ? "no model" : "no report"}
          </Tag>
        ),
    },
  ];

  return (
    <div className="detail-body">
      <header className="detail-head">
        <div>
          <h3>{access.reportName}</h3>
          <p className="muted-note">
            {access.wsName}
            {access.modelName && ` · model: ${access.modelName}`}
            {access.modelMissing && " · model not in scan"}
          </p>
        </div>
        <div className="detail-figures">
          <span className="detail-figure">{num(access.viewers)}</span>
          <span className="muted-note">
            {access.publishedToWeb
              ? "named · plus anyone on the internet"
              : access.orgLink
                ? "named · plus anyone with the link"
                : "can view"}
          </span>
        </div>
      </header>

      {access.modelMissing && (
        <UnavailableNote reason="This report's semantic model was not part of the permission scan — it may live in a workspace that was skipped. Model access cannot be resolved, so nobody is shown as able to view." />
      )}

      <ul className="access-types" aria-label="How people get to this report">
        <li>
          <span className="access-type-n tnum">
            {num(access.byType.workspaceRole)}
          </span>
          <span>
            <strong>Workspace role</strong>
            <span className="muted-note">
              Admin, Member, Contributor or Viewer on the workspace
            </span>
          </span>
        </li>
        <li>
          <span className="access-type-n tnum">
            {num(access.byType.directAccess)}
          </span>
          <span>
            <strong>Direct access</strong>
            <span className="muted-note">
              Granted through Share, or a “Specific people” link — Microsoft
              records both the same way
            </span>
          </span>
        </li>
        <li className={access.orgLink ? "on" : ""}>
          <span className="access-type-n">
            <Link2 size={15} strokeWidth={2.2} aria-hidden="true" />
          </span>
          <span>
            <strong>
              {access.orgLink
                ? "Link for everyone in the organisation"
                : "No organisation-wide link"}
            </strong>
            <span className="muted-note">
              {access.orgLink
                ? `Anyone in your tenant who has the link can open it (${access.orgLink.right})${
                    access.orgLink.sharer
                      ? ` · created by ${access.orgLink.sharer}`
                      : ""
                  }. Not countable as people.`
                : "Nobody gets in through a “People in your organization” link."}
            </span>
          </span>
        </li>
        {access.publishedToWeb && (
          <li className="on danger">
            <span className="access-type-n">
              <Globe size={15} strokeWidth={2.2} aria-hidden="true" />
            </span>
            <span>
              <strong>Published to web — public</strong>
              <span className="muted-note">
                Anyone on the internet with the link, no sign-in
                {access.publishedToWeb.sharer
                  ? ` · published by ${access.publishedToWeb.sharer}`
                  : ""}
                .
              </span>
            </span>
          </li>
        )}
      </ul>

      <div
        className="toolbar"
        style={{ margin: "10px 0", justifyContent: "space-between" }}
      >
        <div className="segmented" role="group" aria-label="Access type">
          {(
            [
              ["all", "Everyone"],
              ["workspace_role", "Workspace role"],
              ["direct_access", "Direct access"],
            ] as [TypeFilter, string][]
          ).map(([value, label]) => (
            <button
              key={value}
              className={type === value ? "seg active" : "seg"}
              aria-pressed={type === value}
              onClick={() => setType(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <label className="toggle">
          <input
            type="checkbox"
            checked={showBlocked}
            onChange={(e) => setShowBlocked(e.target.checked)}
          />
          Show principals with partial access
        </label>
      </div>

      <DataTable
        dense
        columns={pathCols}
        rows={rows}
        rowKey={(p) =>
          p.principal.upn || p.principal.graphId || p.principal.name
        }
        initialSort="who"
        initialDir="asc"
        canExpand={(p) =>
          p.principal.type === "Group" && groupMembers(p).length > 0
        }
        expand={(p) => (
          <div className="nested">
            <div className="nested-title">
              {num(groupMembers(p).length)} people in {p.principal.name} — each
              of them reaches this report through the group
            </div>
            <table className="data-table dense">
              <tbody>
                {groupMembers(p).map((m) => (
                  <tr key={m.id || m.upn || m.name}>
                    <td>
                      <span className="strong">{m.name}</span>
                      {m.upn && <div className="muted-note">{m.upn}</div>}
                    </td>
                    <td style={{ width: 300 }}>
                      <PathText grant={p.reportGrant} />
                    </td>
                    <td style={{ width: 300 }}>
                      <PathText grant={p.modelGrant} />
                    </td>
                    <td style={{ width: 110 }}>
                      {p.canView ? (
                        <Tag tone="good">Yes</Tag>
                      ) : (
                        <Tag tone="critical">No</Tag>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        empty="Nobody holds access to this report."
      />
      {rows.length === 0 && (
        <p className="muted-note">
          Nobody can view this report. Tick the box above to see who holds
          partial access.
        </p>
      )}
    </div>
  );
}

function PersonDetail({
  person,
  onOpenReport,
}: {
  person: import("../lib/access").PrincipalAccess;
  onOpenReport: (id: string) => void;
}) {
  return (
    <div className="detail-body">
      <header className="detail-head">
        <div className="person">
          <Avatar initials={initials(person.principal.name)} />
          <div>
            <h3>{person.principal.name}</h3>
            <p className="muted-note">
              {person.principal.upn || "no UPN"}
              {person.principal.type === "Group" && " · security group"}
            </p>
          </div>
        </div>
        <div className="detail-figures">
          <span className="detail-figure">{num(person.reports.length)}</span>
          <span className="muted-note">reports visible</span>
        </div>
      </header>

      {person.workspaces.length > 0 && (
        <div className="ws-roles">
          <span className="muted-note">Workspace roles:</span>
          {person.workspaces.slice(0, 12).map((w) => (
            <Tag key={w.name} tone={w.right === "Admin" ? "accent" : "neutral"}>
              {w.name} · {w.right}
            </Tag>
          ))}
          {person.workspaces.length > 12 && (
            <span className="muted-note">
              and {person.workspaces.length - 12} more
            </span>
          )}
        </div>
      )}

      <DataTable
        dense
        columns={[
          {
            key: "rep",
            label: "Report",
            value: (r) => r.reportName.toLowerCase(),
            render: (r) => (
              <>
                <span className="strong link-like">{r.reportName}</span>
                <div className="muted-note">{r.wsName}</div>
              </>
            ),
          },
          {
            key: "via",
            label: "Report access via",
            sortable: false,
            render: (r) => <PathText grant={r.reportGrant} />,
          },
          {
            key: "mod",
            label: "Model access via",
            sortable: false,
            render: (r) => <PathText grant={r.modelGrant} />,
          },
        ]}
        rows={person.reports}
        rowKey={(r) => r.reportId}
        onRowClick={(r) => onOpenReport(r.reportId)}
        initialSort="rep"
        initialDir="asc"
        empty="No reports visible to this principal."
      />

      {person.reports.length === 0 && (
        <p className="muted-note">
          This principal cannot view any report — they may hold access to
          reports without access to the semantic models behind them.
        </p>
      )}

      {person.blocked.length > 0 && (
        <div className="blocked-note">
          <strong>
            {person.blocked.length} reports they can open but not read
          </strong>
          <p className="muted-note">
            They have access to the report but not to its semantic model, so the
            report will fail to render for them.
          </p>
          <ul className="evidence">
            {person.blocked.slice(0, 8).map((b) => (
              <li key={b.reportId}>
                {b.reportName} · {b.wsName}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export { sourceLabel };

/**
 * One report's (or one semantic model's reports') access, for the Data
 * Catalog's Access tab: the same scan and the same detail as "Who sees what",
 * without leaving the page.
 */
export function PowerBiAccess({
  reportId,
  modelId,
}: {
  reportId?: string | null;
  modelId?: string | null;
}) {
  const [perm, setPerm] = useState<PermissionScan | null | undefined>(undefined);
  const [picked, setPicked] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    audienceApi
      .getPermissions()
      .then((p) => !cancelled && setPerm(p.scanned ? (p as PermissionScan) : null))
      .catch(() => !cancelled && setPerm(null));
    return () => {
      cancelled = true;
    };
  }, []);
  const index = useMemo(() => (perm ? buildAccessIndex(perm) : null), [perm]);

  if (perm === undefined) {
    return (
      <Card title="Who can see it">
        <Loader2 className="spin" size={16} aria-hidden="true" />
      </Card>
    );
  }
  if (!index) {
    return (
      <Card title="Who can see it">
        <p className="muted-note">
          Access to reports and semantic models is collected by the scan. Run a scan (Rescan, top
          right) and it appears here.
        </p>
      </Card>
    );
  }

  const reports = reportId
    ? [index.byReport.get(reportId)].filter((r): r is ReportAccess => Boolean(r))
    : index.reportList.filter((r) => r.modelId === modelId);
  if (reports.length === 0) {
    return (
      <Card title="Who can see it">
        <p className="muted-note">
          {reportId
            ? "This report was not in the last access scan."
            : "No report in the scanned workspaces uses this semantic model, so nobody sees its data through a report."}
        </p>
      </Card>
    );
  }
  const current = reports.find((r) => r.reportId === picked) ?? reports[0];
  return (
    <Card
      title="Who can see it"
      subtitle={
        reportId
          ? "A person sees a report only with read on both the report and its semantic model."
          : reports.length === 1
            ? "One report uses this model. A person sees it only with read on both the report and the model."
            : `${num(reports.length)} reports use this model. Pick one to see who can view it.`
      }
    >
      {reports.length > 1 && (
        <div className="segmented power-bi-access-pick" role="group" aria-label="Report">
          {reports.map((r) => (
            <button
              key={r.reportId}
              type="button"
              className={r.reportId === current.reportId ? "active" : ""}
              onClick={() => setPicked(r.reportId)}
            >
              {r.reportName}
            </button>
          ))}
        </div>
      )}
      <ReportDetail key={current.reportId} access={current} index={index} />
    </Card>
  );
}
