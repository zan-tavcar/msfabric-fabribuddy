import { type FormEvent, useEffect, useMemo, useState } from "react";
import { CheckCircle2, CircleAlert, Loader2, Plus, Search, Trash2, XCircle } from "lucide-react";
import { ApiError, api } from "../api/client";
import type {
  AdminTenant,
  AdminWorkspace,
  CreatedInvitation,
  Me,
  MembersPayload,
  Role,
  ScanPass,
  ScheduleIn,
  ScopeMode,
  Subscription,
} from "../api/types";
import { Card, Chip, SectionHead } from "../components/primitives";
import { duration } from "../components/ScanProgress";
import { stamp } from "../lib/format";
import { signInReturn } from "../lib/signInReturn";
import { PlanSection } from "./PlanSection";

type Section =
  "connection" | "prerequisites" | "workspaces" | "schedule" | "members" | "plan" | "account";

const SECTIONS: [Section, string][] = [
  ["connection", "Connection"],
  ["prerequisites", "Prerequisites"],
  ["workspaces", "Workspaces"],
  ["schedule", "Scan schedule"],
  ["members", "Members"],
  ["plan", "Plan"],
  ["account", "Account"],
];

/** No account (FL_AUTH_REQUIRED=0) means a single local operator: an admin. */
export const isAdmin = (me: Me | null) => !me || me.role === "admin";
/** May write table and column descriptions: an admin or a contributor. */
export const canDocument = (me: Me | null) =>
  !me || me.role === "admin" || me.role === "contributor";

export const ROLE_LABELS: Record<Role, string> = {
  admin: "Admin",
  contributor: "Contributor",
  viewer: "Viewer",
};

const MAX_SCANS = 8;
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

/**
 * Everything the organisation configures: the service principal, which
 * workspaces are scanned, when, and the signed-in user's account.
 *
 * The client secret is write-only — the page can replace it, never show it.
 */
export function AdminPage({
  me,
  onSignedOut,
  onTenantChanged,
  initialSection,
  subscription,
  onSubscriptionChanged,
}: {
  me: Me | null;
  onSignedOut: () => void;
  onTenantChanged: () => void;
  initialSection?: Section;
  subscription: Subscription | null;
  onSubscriptionChanged: (next: Subscription) => void;
}) {
  const [tenant, setTenant] = useState<AdminTenant | null>(null);
  const [section, setSection] = useState<Section>(initialSection ?? "connection");
  const [error, setError] = useState<string | null>(null);
  const admin = isAdmin(me);

  useEffect(() => {
    if (!admin) return;
    api
      .adminTenant()
      .then(setTenant)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [admin]);

  // A viewer or contributor manages only their own account.
  if (!admin) {
    return (
      <div className="stack">
        <div className="page-head">
          <h1 className="page-title">Account</h1>
          <p className="page-lede">
            You have read access. The connection, scanned workspaces, schedule and members are
            managed by your organisation's admins.
          </p>
        </div>
        <AccountSection me={me} onSignedOut={onSignedOut} />
      </div>
    );
  }

  const changed = (next: AdminTenant) => {
    setTenant(next);
    onTenantChanged();
  };

  return (
    <div className="stack">
      <div className="page-head">
        <h1 className="page-title">Settings</h1>
        <p className="page-lede">
          One place for both products: connect your tenant, check the prerequisites, choose what is
          scanned and when, pick your plan and invite your team. Read-only against your tenant —
          metadata only, never row data.
        </p>
      </div>
      {error && <p className="banner banner-danger">{error}</p>}
      {tenant && !tenant.connected && (
        <p className="banner banner-warn">
          No tenant connected yet. Work through the <strong>Prerequisites</strong>, then enter the
          service principal under <strong>Connection</strong>.
        </p>
      )}

      <nav className="tabs" aria-label="Admin sections">
        {SECTIONS.map(([id, label]) => (
          <button
            key={id}
            type="button"
            className={section === id ? "tab active" : "tab"}
            onClick={() => setSection(id)}
          >
            {label}
          </button>
        ))}
      </nav>

      {tenant === null ? (
        <p className="muted-note">Loading…</p>
      ) : (
        <>
          {section === "connection" && (
            <ConnectionSection tenant={tenant} onChanged={changed} onGo={setSection} />
          )}
          {section === "prerequisites" && <Prerequisites tenant={tenant} />}
          {section === "workspaces" && <WorkspacesSection tenant={tenant} onChanged={changed} />}
          {section === "schedule" && (
            <ScheduleSection
              tenant={tenant}
              onChanged={changed}
              onScanStarted={onTenantChanged}
              maxScans={Math.min(MAX_SCANS, subscription?.limits.dailyScans ?? MAX_SCANS)}
            />
          )}
          {section === "members" && <MembersSection />}
          {section === "plan" && (
            <PlanSection subscription={subscription} onChanged={onSubscriptionChanged} />
          )}
          {section === "account" && <AccountSection me={me} onSignedOut={onSignedOut} />}
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------ connection */

function ConnectionSection({
  tenant,
  onChanged,
  onGo,
}: {
  tenant: AdminTenant;
  onChanged: (t: AdminTenant) => void;
  onGo: (s: Section) => void;
}) {
  const [tenantId, setTenantId] = useState(tenant.tenantId ?? "");
  const [clientId, setClientId] = useState(tenant.clientId ?? "");
  const [secret, setSecret] = useState("");
  const [name, setName] = useState(tenant.displayName ?? "");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (label: string, work: () => Promise<AdminTenant | void>) => {
    setBusy(label);
    setError(null);
    try {
      const next = await work();
      if (next) onChanged(next);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const save = (event: FormEvent) => {
    event.preventDefault();
    run("save", async () => {
      const next = await api.saveTenant({
        tenantId: tenantId.trim(),
        clientId: clientId.trim(),
        clientSecret: secret,
        displayName: name.trim(),
      });
      setSecret("");
      return next;
    });
  };

  return (
    <div className="admin-grid">
      <Card className="admin-card">
        <SectionHead
          title="Service principal"
          sub="An Entra app registration the product signs in as. Saving validates it against Microsoft."
        />
        <form className="form-stack" onSubmit={save}>
          <label className="form-field" htmlFor="c-name">
            <span className="form-label">Display name</span>
            <input
              id="c-name"
              className="input"
              value={name}
              placeholder="Contoso"
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="form-field" htmlFor="c-tenant">
            <span className="form-label">Directory (tenant) ID</span>
            <input
              id="c-tenant"
              className="input mono"
              value={tenantId}
              placeholder="00000000-0000-0000-0000-000000000000"
              onChange={(e) => setTenantId(e.target.value)}
              required
            />
          </label>
          <label className="form-field" htmlFor="c-client">
            <span className="form-label">Application (client) ID</span>
            <input
              id="c-client"
              className="input mono"
              value={clientId}
              placeholder="00000000-0000-0000-0000-000000000000"
              onChange={(e) => setClientId(e.target.value)}
              required
            />
          </label>
          <label className="form-field" htmlFor="c-secret">
            <span className="form-label">Client secret</span>
            <input
              id="c-secret"
              className="input mono"
              type="password"
              autoComplete="new-password"
              value={secret}
              placeholder={
                tenant.secretSet ? "Stored — leave empty to keep it" : "The secret's value"
              }
              onChange={(e) => setSecret(e.target.value)}
            />
            <span className="form-hint">
              Stored encrypted and never shown again. Enter a new one to rotate it.
            </span>
          </label>
          {error && <p className="banner banner-danger">{error}</p>}
          <div className="toolbar">
            <button type="submit" className="btn btn-primary" disabled={busy !== null}>
              {busy === "save" && <Loader2 className="spin" size={14} aria-hidden="true" />}
              {tenant.connected ? "Save & validate" : "Connect"}
            </button>
          </div>
        </form>
      </Card>

      <Card className="admin-card">
        <SectionHead
          title="What the connection can do"
          sub={
            tenant.lastValidatedAt
              ? `Last checked ${stamp(tenant.lastValidatedAt)}`
              : "Not validated yet"
          }
        />
        {!tenant.connected ? (
          <p className="muted-note">
            Connect to see which access tiers work.{" "}
            <button type="button" className="link" onClick={() => onGo("prerequisites")}>
              Read the prerequisites
            </button>
            .
          </p>
        ) : (
          <ul className="check-list">
            <Capability
              ok={tenant.capabilities?.powerbi_admin?.available}
              title="Read-only admin APIs"
              detail="Workspaces, items, users, semantic-model schemas. Required."
              reason={tenant.capabilities?.powerbi_admin?.reason}
            />
            <Capability
              ok={tenant.capabilities?.fabric_api?.available}
              title="Fabric REST API"
              detail="Item inventory and definitions (notebooks, pipelines, dataflows)."
              reason={tenant.capabilities?.fabric_api?.reason}
            />
            <Capability
              ok={tenant.capabilities?.onelake?.available}
              title="OneLake"
              detail="Data Catalog: table schemas and the Delta log."
              reason={tenant.capabilities?.onelake?.reason}
            />
            <Capability
              ok={tenant.capabilities?.graph_licenses?.available}
              title="Microsoft Graph"
              detail="Report Audience: Pro / PPU licences and Entra group members. Optional."
              reason={tenant.capabilities?.graph_licenses?.reason}
            />
          </ul>
        )}
        {tenant.connected && (
          <div className="revalidate">
            <button
              type="button"
              className="btn"
              disabled={busy !== null}
              onClick={() => run("validate", () => api.validateTenant())}
            >
              {busy === "validate" && <Loader2 className="spin" size={14} aria-hidden="true" />}
              Re-validate
            </button>
            <span className="muted-note">
              Changed a tenant setting or permission? Microsoft can take 15–60 minutes to apply
              it — check again here.
            </span>
          </div>
        )}
        {tenant.connected && (
          <div className="danger-zone">
            <strong>Disconnect</strong>
            <p className="muted-note">
              Removes the stored secret and every scan result for this tenant.
            </p>
            <button
              type="button"
              className="btn btn-danger"
              disabled={busy !== null}
              onClick={() => {
                if (
                  window.confirm(
                    "Disconnect this tenant? The stored secret and all collected metadata are deleted.",
                  )
                ) {
                  run("disconnect", async () => {
                    await api.disconnectTenant();
                    return { connected: false };
                  });
                }
              }}
            >
              <Trash2 size={14} aria-hidden="true" /> Disconnect tenant
            </button>
          </div>
        )}
      </Card>
    </div>
  );
}

function Capability({
  ok,
  title,
  detail,
  reason,
}: {
  ok?: boolean;
  title: string;
  detail: string;
  reason?: string;
}) {
  return (
    <li className="check-item">
      {ok ? (
        <CheckCircle2 size={18} className="ok" aria-label="Available" />
      ) : (
        <XCircle size={18} className="bad" aria-label="Not available" />
      )}
      <div>
        <strong>{title}</strong>
        <p className="muted-note">{detail}</p>
        {!ok && reason && <p className="check-reason">{reason}</p>}
      </div>
    </li>
  );
}

/* --------------------------------------------------------- prerequisites */

function Prerequisites({ tenant }: { tenant: AdminTenant }) {
  const caps = tenant.capabilities ?? {};
  return (
    <div className="stack-sm">
      <Card className="admin-card">
        <SectionHead
          title="Before you connect"
          sub="One-time setup in Microsoft Entra and the Fabric admin portal — about 15 minutes."
        />
        <ol className="prereq-list">
          <Step
            n={1}
            title="Create an app registration"
            done={tenant.connected}
            body={
              <p>
                <em>Entra ID → App registrations → New registration.</em> Any name, no redirect URI.
                Then <em>Certificates &amp; secrets → New client secret</em> and copy the{" "}
                <strong>value</strong> (shown once). Note the{" "}
                <strong>Application (client) ID</strong> and <strong>Directory (tenant) ID</strong>.
              </p>
            }
          />
          <Step
            n={2}
            title="Allow service principals to use read-only admin APIs"
            done={caps.powerbi_admin?.available}
            body={
              <>
                <p>
                  Create a security group (e.g. <code>sg-fabribuddy</code>) and add the service
                  principal. In <em>Fabric admin portal → Tenant settings</em>, enable{" "}
                  <strong>Service principals can use read-only admin APIs</strong> for that group.
                </p>
                <p className="muted-note">Tenant settings take up to 15 minutes to apply.</p>
              </>
            }
          />
          <Step
            n={3}
            title="Report Audience: add Microsoft Graph permissions (optional)"
            done={caps.graph_licenses?.available}
            body={
              <>
                <p>
                  <em>API permissions → Add → Microsoft Graph → Application permissions</em>:{" "}
                  <code>User.Read.All</code>, <code>Organization.Read.All</code>,{" "}
                  <code>GroupMember.Read.All</code>, then <em>Grant admin consent</em>.
                </p>
                <p className="muted-note">
                  Without them Report Audience still scans, but cannot list Pro / PPU licences, and
                  a grant to an Entra group shows as the group rather than the people in it.
                </p>
              </>
            }
          />
          <Step
            n={4}
            title="Give the principal Contributor on the workspaces you want scanned"
            done={(tenant.scope?.effective ?? 0) > 0}
            body={
              <>
                <p>
                  <em>
                    Workspace → Manage access → Add people or groups → the service principal →
                    Contributor
                  </em>
                  . Report Audience needs no workspace role at all: it reads through the admin APIs.
                  Choose what is scanned under <strong>Workspaces</strong>.
                </p>
                <p className="muted-note">
                  Why Contributor for a read-only product: Fabric's <code>getDefinition</code> API
                  (the only way to read notebook, pipeline and dataflow code) requires it, and
                  OneLake and the warehouse SQL endpoints need workspace access. Nothing here ever
                  creates, changes or deletes anything in your tenant — every call is a read.
                </p>
              </>
            }
          />
          <Step
            n={5}
            title="Connect and scan"
            done={tenant.connected && tenant.status === "connected"}
            body={
              <p>
                Enter the three values under <strong>Connection</strong>. The first full scan takes
                a few minutes per hundred items; later scheduled scans are incremental.
              </p>
            }
          />
        </ol>
      </Card>
    </div>
  );
}

function Step({
  n,
  title,
  body,
  done,
}: {
  n: number;
  title: string;
  body: React.ReactNode;
  done?: boolean;
}) {
  return (
    <li className="prereq-step">
      <span className={done ? "prereq-num done" : "prereq-num"} aria-hidden="true">
        {done ? <CheckCircle2 size={16} /> : n}
      </span>
      <div>
        <strong>{title}</strong>
        {done !== undefined && (
          <Chip tone={done ? "green" : "neutral"}>{done ? "Done" : "To do"}</Chip>
        )}
        <div className="prereq-body">{body}</div>
      </div>
    </li>
  );
}

/* ------------------------------------------------------------ workspaces */

function WorkspacesSection({
  tenant,
  onChanged,
}: {
  tenant: AdminTenant;
  onChanged: (t: AdminTenant) => void;
}) {
  const [rows, setRows] = useState<AdminWorkspace[] | null>(null);
  const [meta, setMeta] = useState<{
    cap: number | null;
    includePersonal: boolean;
    fabricError: string | null;
  } | null>(null);
  const [includePersonal, setIncludePersonal] = useState(false);
  const [mode, setMode] = useState<ScopeMode>(tenant.scope?.mode ?? "all");
  const [chosen, setChosen] = useState<Set<string>>(new Set(tenant.scope?.workspaceIds ?? []));
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!tenant.connected) return;
    api
      .adminWorkspaces()
      .then((r) => {
        setRows(r.workspaces);
        setMeta({
          cap: r.cap,
          includePersonal: r.includePersonal,
          fabricError: r.fabricError,
        });
        setIncludePersonal(r.includePersonal);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [tenant.connected]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (rows ?? []).filter((w) => !needle || w.name.toLowerCase().includes(needle));
  }, [rows, query]);
  const scannableRows = visible.filter((w) => w.lineage || w.audience);

  if (!tenant.connected) {
    return <p className="muted-note">Connect a tenant first.</p>;
  }

  const toggle = (id: string) => {
    setSaved(false);
    setChosen((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const setMany = (ids: string[], on: boolean) => {
    setSaved(false);
    setChosen((current) => {
      const next = new Set(current);
      ids.forEach((id) => (on ? next.add(id) : next.delete(id)));
      return next;
    });
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      onChanged(await api.saveScope(mode, [...chosen], includePersonal));
      setSaved(true);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const lineageCount = (rows ?? []).filter((w) => w.lineage).length;
  const audienceCount = (rows ?? []).filter((w) => w.audience).length;
  // Computed against the saved scope; shown only while the workspace is unticked.
  const missing = (rows ?? []).filter((w) => w.referencedBy?.length && !chosen.has(w.id));

  return (
    <Card className="admin-card">
      <SectionHead
        title="Which workspaces are scanned"
        sub={`${lineageCount} readable by Data Catalog (Contributor) · ${audienceCount} by Report Audience (admin API). One selection serves both products and is what your plan counts.`}
      />
      <div className="radio-row" role="radiogroup" aria-label="Scope">
        <label className="radio">
          <input
            type="radio"
            name="scope"
            checked={mode === "all"}
            onChange={() => {
              setMode("all");
              setSaved(false);
            }}
          />
          <span>
            <strong>All</strong> — every workspace where it is Contributor, including new ones
          </span>
        </label>
        <label className="radio">
          <input
            type="radio"
            name="scope"
            checked={mode === "selected"}
            onChange={() => {
              setMode("selected");
              setSaved(false);
            }}
          />
          <span>
            <strong>Selected</strong> — only the ones ticked below ({chosen.size})
          </span>
        </label>
      </div>

      <div className="toolbar ws-tools">
        <span className="filter-search-box ws-search">
          <Search size={14} strokeWidth={2.2} aria-hidden="true" />
          <input
            className="input"
            type="search"
            placeholder="Search workspaces"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search workspaces"
          />
        </span>
        <button
          type="button"
          className="btn"
          disabled={mode !== "selected"}
          onClick={() =>
            setMany(
              scannableRows.map((w) => w.id),
              true,
            )
          }
        >
          Select all{query ? " shown" : ""}
        </button>
        <button
          type="button"
          className="btn"
          disabled={mode !== "selected"}
          onClick={() =>
            setMany(
              visible.map((w) => w.id),
              false,
            )
          }
        >
          Clear{query ? " shown" : ""}
        </button>
      </div>

      {mode === "selected" && missing.length > 0 && (
        <div className="banner banner-warn">
          <p>
            <strong>
              {missing.length === 1
                ? "1 unselected workspace is"
                : `${missing.length} unselected workspaces are`}{" "}
              read or written by code you selected.
            </strong>{" "}
            Its tables are not scanned, so lineage from that code stops short:{" "}
            {missing.map((w) => w.name).join(", ")}.
          </p>
          <button
            type="button"
            className="btn"
            onClick={() =>
              setMany(
                missing.map((w) => w.id),
                true,
              )
            }
          >
            Add {missing.length === 1 ? "it" : "them"} to the selection
          </button>
        </div>
      )}

      {meta?.cap != null && (
        <p className={chosen.size > meta.cap ? "banner banner-danger" : "banner banner-warn"}>
          Your plan scans up to {meta.cap} workspaces for both products ({chosen.size} selected).
          Plans are priced by workspaces: Team 10, Business 40, Enterprise unlimited, under{" "}
          <strong>Plan</strong>.
        </p>
      )}
      {meta?.fabricError && (
        <p className="banner banner-warn">
          Could not ask Fabric which workspaces the principal has a role on ({meta.fabricError}).
          Listing Report Audience's inventory only.
        </p>
      )}
      <label className="radio">
        <input
          type="checkbox"
          checked={includePersonal}
          onChange={(e) => {
            setIncludePersonal(e.target.checked);
            setSaved(false);
          }}
        />
        <span>
          Report Audience: include personal <em>My workspace</em> workspaces (usually noise for an
          audit)
        </span>
      </label>

      {rows === null && !error ? (
        <p className="muted-note">Asking Fabric which workspaces the principal can reach…</p>
      ) : (
        <div className="ws-list" role="list">
          {visible.map((w) => {
            const scannable = w.lineage || w.audience;
            const disabled = mode !== "selected" || !scannable;
            return (
              <label key={w.id} className={disabled ? "ws-row disabled" : "ws-row"} role="listitem">
                <input
                  type="checkbox"
                  checked={mode === "all" ? scannable : chosen.has(w.id)}
                  disabled={disabled}
                  onChange={() => toggle(w.id)}
                />
                <span className="ws-name">{w.name}</span>
                {w.referencedBy?.length && !chosen.has(w.id) ? (
                  <span className="ws-used" title={w.referencedBy.join(", ")}>
                    used by {w.referencedBy.length} selected item
                    {w.referencedBy.length === 1 ? "" : "s"}
                  </span>
                ) : null}
                {w.access === "contributor" ? (
                  <Chip tone="green">Data Catalog · Contributor</Chip>
                ) : w.access === "viewer" ? (
                  <Chip tone="neutral">Data Catalog: Viewer only — needs Contributor</Chip>
                ) : (
                  <Chip tone="neutral">Data Catalog: no role</Chip>
                )}
                {w.audience ? (
                  <Chip tone="green">Report Audience</Chip>
                ) : (
                  <Chip tone="neutral">Report Audience: after its first scan</Chip>
                )}
              </label>
            );
          })}
          {visible.length === 0 && <p className="muted-note">No workspace matches.</p>}
        </div>
      )}

      {error && <p className="banner banner-danger">{error}</p>}
      <div className="toolbar">
        <button type="button" className="btn btn-primary" disabled={busy} onClick={save}>
          {busy && <Loader2 className="spin" size={14} aria-hidden="true" />}
          Save scope
        </button>
        {saved && <span className="muted-note">Saved — applies from the next scan.</span>}
      </div>
    </Card>
  );
}

/* -------------------------------------------------------------- schedule */

function ScheduleSection({
  tenant,
  onChanged,
  onScanStarted,
  maxScans,
}: {
  tenant: AdminTenant;
  onChanged: (t: AdminTenant) => void;
  /** Lets the app shell poll now, so the page refreshes as stages finish. */
  onScanStarted: () => void;
  /** Scheduled slots a day on the organisation's plan. */
  maxScans: number;
}) {
  const initial = tenant.schedule;
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const [enabled, setEnabled] = useState(initial?.enabled ?? false);
  const [times, setTimes] = useState<string[]>(initial?.times?.length ? initial.times : ["06:00"]);
  const [timezone, setTimezone] = useState(
    initial?.timezone && initial.timezone !== "UTC" ? initial.timezone : browserZone,
  );
  const [fullWeekday, setFullWeekday] = useState<number | null>(initial?.fullWeekday ?? 6);
  const [zones, setZones] = useState<string[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .timezones()
      .then(setZones)
      .catch(() => setZones([browserZone]));
  }, [browserZone]);

  if (!tenant.connected) return <p className="muted-note">Connect a tenant first.</p>;

  const save = async () => {
    setBusy("save");
    setError(null);
    setMessage(null);
    try {
      const body: ScheduleIn = { enabled, times, timezone, fullWeekday };
      onChanged(await api.saveSchedule(body));
      setMessage("Schedule saved.");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const scan = async (full: boolean) => {
    setBusy(full ? "full" : "inc");
    setError(null);
    setMessage(null);
    try {
      await api.scan(full);
      onScanStarted();
      setMessage(
        full
          ? "Full refresh started — every definition is re-read. This takes a while."
          : "Incremental scan started.",
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="admin-grid">
      <Card className="admin-card">
        <SectionHead
          title="Scheduled scans"
          sub={`Up to ${maxScans} a day on your plan. Scheduled scans are incremental: only changed items are re-read.`}
        />
        <label className="switch">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          <span>Scan automatically</span>
        </label>

        <div className="time-list">
          {times.map((time, index) => (
            <div key={index} className="time-row">
              <input
                type="time"
                className="input"
                value={time}
                aria-label={`Scan time ${index + 1}`}
                onChange={(e) =>
                  setTimes((current) => current.map((t, i) => (i === index ? e.target.value : t)))
                }
              />
              <button
                type="button"
                className="btn btn-icon"
                aria-label="Remove this time"
                disabled={times.length === 1}
                onClick={() => setTimes((current) => current.filter((_, i) => i !== index))}
              >
                <Trash2 size={14} aria-hidden="true" />
              </button>
            </div>
          ))}
          <button
            type="button"
            className="btn"
            disabled={times.length >= maxScans}
            onClick={() => setTimes((current) => [...current, "18:00"])}
          >
            <Plus size={14} aria-hidden="true" /> Add a time ({times.length}/{maxScans})
          </button>
        </div>

        <label className="form-field" htmlFor="s-tz">
          <span className="form-label">Time zone</span>
          <select
            id="s-tz"
            className="input"
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
          >
            {(zones.length ? zones : [timezone]).map((z) => (
              <option key={z} value={z}>
                {z}
              </option>
            ))}
          </select>
        </label>

        <label className="form-field" htmlFor="s-full">
          <span className="form-label">Weekly full refresh</span>
          <select
            id="s-full"
            className="input"
            value={fullWeekday === null ? "" : String(fullWeekday)}
            onChange={(e) => setFullWeekday(e.target.value === "" ? null : Number(e.target.value))}
          >
            <option value="">Never</option>
            {WEEKDAYS.map((day, i) => (
              <option key={day} value={i}>
                {day} — first scan of the day re-reads everything
              </option>
            ))}
          </select>
        </label>

        {error && <p className="banner banner-danger">{error}</p>}
        {message && <p className="banner banner-ok">{message}</p>}
        <div className="toolbar">
          <button type="button" className="btn btn-primary" disabled={busy !== null} onClick={save}>
            {busy === "save" && <Loader2 className="spin" size={14} aria-hidden="true" />}
            Save schedule
          </button>
        </div>
        <p className="muted-note">
          Last scheduled scan: {stamp(tenant.schedule?.lastScheduledAt ?? null)}
        </p>
      </Card>

      <Card className="admin-card">
        <SectionHead title="Scan now" sub="Runs every collector over the selected workspaces." />
        <div className="stack-sm">
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy !== null}
            onClick={() => scan(false)}
          >
            {busy === "inc" && <Loader2 className="spin" size={14} aria-hidden="true" />}
            Incremental scan
          </button>
          <p className="muted-note">
            Re-reads only items changed since the last scan — usually a minute or two.
          </p>
          <button type="button" className="btn" disabled={busy !== null} onClick={() => scan(true)}>
            {busy === "full" && <Loader2 className="spin" size={14} aria-hidden="true" />}
            Full refresh
          </button>
          <p className="muted-note">
            Re-reads every definition. Use after changing parsers or when something looks stale.
          </p>
        </div>
      </Card>
      <RecentScans />
    </div>
  );
}

const SCAN_STATUS: Record<ScanPass["status"], { label: string; tone: string }> = {
  ok: { label: "Succeeded", tone: "green" },
  partial: { label: "Partial", tone: "amber" },
  failed: { label: "Failed", tone: "danger" },
  running: { label: "Running", tone: "blue" },
};

/** This organisation's last scan passes: when, how long, how they ended. */
function RecentScans() {
  const [rows, setRows] = useState<ScanPass[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const next = await api.scanHistory(10).catch(() => null);
      if (cancelled) return;
      if (next) setRows(next);
      timer = setTimeout(tick, next?.some((r) => r.status === "running") ? 5000 : 30000);
    };
    void tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, []);

  return (
    <Card className="admin-card admin-card-wide">
      <SectionHead
        title="Recent scans"
        sub="Each scan reads Data Catalog, then Report Audience, over the same workspaces."
      />
      {!rows ? (
        <p className="muted-note">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="muted-note">No scans yet.</p>
      ) : (
        <table className="recent-scans">
          <thead>
            <tr>
              <th>Started</th>
              <th>Duration</th>
              <th>Trigger</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="tnum">{stamp(r.startedAt)}</td>
                <td className="tnum">
                  {r.status === "running" ? "running…" : duration(r.durationSeconds)}
                </td>
                <td>
                  {{ manual: "Manual", schedule: "Scheduled", api: "API" }[r.trigger] ?? r.trigger}
                  <span className="muted-note"> · {r.incremental ? "incremental" : "full"}</span>
                </td>
                <td>
                  <Chip tone={SCAN_STATUS[r.status]?.tone ?? "neutral"}>
                    {SCAN_STATUS[r.status]?.label ?? r.status}
                  </Chip>
                  {r.stepsFailed > 0 && (
                    <span className="muted-note">
                      {" "}
                      {r.stepsFailed} step{r.stepsFailed === 1 ? "" : "s"} failed
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

/* --------------------------------------------------------------- members */

function MembersSection() {
  const [data, setData] = useState<MembersPayload | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [created, setCreated] = useState<CreatedInvitation | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    api
      .members()
      .then(setData)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));

  useEffect(() => {
    load();
  }, []);

  const act = async (work: () => Promise<unknown>) => {
    setError(null);
    try {
      await work();
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };

  const invite = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setCreated(null);
    setCopied(false);
    await act(async () => {
      setCreated(await api.invite(email, role));
      setEmail("");
    });
    setBusy(false);
  };

  if (!data) {
    return error ? (
      <p className="banner banner-danger">{error}</p>
    ) : (
      <p className="muted-note">Loading…</p>
    );
  }

  const seatsFull = data.adminSeats.used >= data.adminSeats.max;

  return (
    <div className="admin-grid">
      <Card className="admin-card">
        <SectionHead
          title="Invite someone"
          sub={`Viewers read; contributors also write table and column descriptions; admins manage settings, workspaces and members. Viewers and contributors are unlimited. Admins: ${data.adminSeats.used} of ${data.adminSeats.max} seats used (pending invitations count).`}
        />
        <form className="invite-row" onSubmit={invite}>
          <input
            className="input"
            type="email"
            required
            placeholder="name@company.com"
            aria-label="E-mail address to invite"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <select
            className="input"
            aria-label="Role"
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
          >
            <option value="viewer">Viewer — read-only</option>
            <option value="contributor">Contributor — writes descriptions</option>
            <option value="admin" disabled={seatsFull}>
              Admin{seatsFull ? " — no seats left" : ""}
            </option>
          </select>
          <button type="submit" className="btn btn-primary" disabled={busy || !email}>
            {busy ? (
              <Loader2 className="spin" size={14} aria-hidden="true" />
            ) : (
              <Plus size={14} aria-hidden="true" />
            )}
            Invite
          </button>
        </form>
        {created && (
          <div className="banner banner-ok invite-link">
            <p>
              {created.emailSent
                ? `Invitation e-mailed to ${created.email}. You can also share this link:`
                : `Send this link to ${created.email} — it is shown only now and works once, for 7 days:`}
            </p>
            <div className="invite-copy">
              <input
                className="input mono"
                readOnly
                value={created.link}
                aria-label="Invitation link"
              />
              <button
                type="button"
                className="btn"
                onClick={() =>
                  navigator.clipboard
                    ?.writeText(created.link)
                    .then(() => setCopied(true))
                    .catch(() => undefined)
                }
              >
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
          </div>
        )}
        {!data.emailEnabled && !created && (
          <p className="muted-note">
            E-mail is not configured on this instance (FL_SMTP_HOST), so you pass the link on
            yourself.
          </p>
        )}
      </Card>

      <Card className="admin-card">
        <SectionHead title="Members" sub={`${data.members.length} in your organisation`} />
        <div className="member-list" role="list">
          {data.members.map((m) => (
            <div key={m.id} className="member-row" role="listitem">
              <span className="member-who">
                <strong>{m.displayName || m.email}</strong>
                {m.isYou && <span className="muted-note"> (you)</span>}
                <span className="member-email">{m.email}</span>
              </span>
              <span className="muted-note member-seen">
                {m.lastLoginAt ? `last seen ${stamp(m.lastLoginAt)}` : "never signed in"}
              </span>
              <select
                className="input member-role"
                aria-label={`Role of ${m.email}`}
                value={m.role}
                onChange={(e) => act(() => api.setRole(m.id, e.target.value as Role))}
              >
                <option value="viewer">Viewer</option>
                <option value="contributor">Contributor</option>
                <option value="admin" disabled={seatsFull && m.role !== "admin"}>
                  Admin
                </option>
              </select>
              <button
                type="button"
                className="btn btn-icon"
                aria-label={`Remove ${m.email}`}
                title={m.isYou ? "Remove yourself" : "Remove"}
                onClick={() => {
                  if (window.confirm(`Remove ${m.email}? They lose access immediately.`)) {
                    act(() => api.removeMember(m.id));
                  }
                }}
              >
                <Trash2 size={14} aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>

        {data.invitations.length > 0 && (
          <>
            <h3 className="subhead">Pending invitations</h3>
            <div className="member-list" role="list">
              {data.invitations.map((i) => (
                <div key={i.id} className="member-row" role="listitem">
                  <span className="member-who">
                    <strong>{i.email}</strong>
                  </span>
                  <span className="muted-note member-seen">expires {stamp(i.expiresAt)}</span>
                  <Chip tone={i.role === "admin" ? "amber" : "neutral"}>
                    {ROLE_LABELS[i.role as Role] ?? i.role}
                  </Chip>
                  <button
                    type="button"
                    className="btn btn-icon"
                    aria-label={`Revoke invitation for ${i.email}`}
                    title="Revoke"
                    onClick={() => act(() => api.revokeInvitation(i.id))}
                  >
                    <XCircle size={14} aria-hidden="true" />
                  </button>
                </div>
              ))}
            </div>
          </>
        )}
      </Card>
      {error && (
        <p className="banner banner-danger" role="alert">
          <CircleAlert size={14} aria-hidden="true" /> {error}
        </p>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- account */

function AccountSection({ me, onSignedOut }: { me: Me | null; onSignedOut: () => void }) {
  const [name, setName] = useState(me?.displayName ?? "");
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [message, setMessage] = useState<string | null>(signInReturn?.ok ?? null);
  const [error, setError] = useState<string | null>(signInReturn?.error ?? null);
  // An account made with Microsoft has no password until it sets one.
  const hasPassword = me?.hasPassword !== false;
  const [microsoftOffered, setMicrosoftOffered] = useState(false);
  useEffect(() => {
    api
      .authConfig()
      .then((c) => setMicrosoftOffered(Boolean(c.microsoftSignIn)))
      .catch(() => undefined);
  }, []);

  if (!me) {
    return (
      <p className="muted-note">
        Accounts are off on this instance (FL_AUTH_REQUIRED=0) — there is nothing to manage.
      </p>
    );
  }

  const act = async (work: () => Promise<unknown>, ok: string) => {
    setError(null);
    setMessage(null);
    try {
      await work();
      setMessage(ok);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };

  return (
    <div className="admin-grid">
      <Card className="admin-card">
        <SectionHead title="Profile" sub={me.email} />
        <label className="form-field" htmlFor="p-name">
          <span className="form-label">Display name</span>
          <input
            id="p-name"
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <div className="toolbar">
          <button
            type="button"
            className="btn"
            onClick={() => act(() => api.updateProfile(name), "Profile saved.")}
          >
            Save
          </button>
          <button
            type="button"
            className="btn"
            onClick={async () => {
              await api.logout().catch(() => undefined);
              onSignedOut();
            }}
          >
            Sign out
          </button>
        </div>
      </Card>

      {(microsoftOffered || me.microsoft) && (
        <Card className="admin-card">
          <SectionHead
            title="Sign in with Microsoft"
            sub={
              me.microsoft
                ? "Linked: you can sign in with your Microsoft work account."
                : "Sign in with your Microsoft work account instead of a password."
            }
          />
          {!me.microsoft && (
            <div className="toolbar">
              <button
                type="button"
                className="btn"
                onClick={() =>
                  act(async () => {
                    window.location.assign((await api.microsoftLink()).url);
                  }, "Opening Microsoft…")
                }
              >
                Link Microsoft account
              </button>
            </div>
          )}
        </Card>
      )}

      <Card className="admin-card">
        <SectionHead
          title={hasPassword ? "Change password" : "Set a password"}
          sub={
            hasPassword
              ? "Signs out every other browser."
              : "Optional: you sign in with Microsoft. A password adds a second way in."
          }
        />
        <form
          className="form-stack"
          onSubmit={(e) => {
            e.preventDefault();
            if (next !== confirm) {
              setError("The new passwords do not match.");
              return;
            }
            act(async () => {
              await api.changePassword(current, next);
              setCurrent("");
              setNext("");
              setConfirm("");
            }, "Password changed.");
          }}
        >
          {hasPassword && (
            <input
              className="input"
              type="password"
              autoComplete="current-password"
              placeholder="Current password"
              aria-label="Current password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
            />
          )}
          <input
            className="input"
            type="password"
            autoComplete="new-password"
            placeholder="New password"
            aria-label="New password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
          <input
            className="input"
            type="password"
            autoComplete="new-password"
            placeholder="Repeat the new password"
            aria-label="Repeat the new password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
          <span className="form-hint">
            At least 10 characters, with upper- and lower-case letters, a digit and a special character.
          </span>
          <button type="submit" className="btn btn-primary">
            {hasPassword ? "Change password" : "Set password"}
          </button>
        </form>
      </Card>
      {error && (
        <p className="banner banner-danger" role="alert">
          <CircleAlert size={14} aria-hidden="true" /> {error}
        </p>
      )}
      {message && <p className="banner banner-ok">{message}</p>}
    </div>
  );
}
