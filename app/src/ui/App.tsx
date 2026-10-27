import { useCallback, useEffect, useRef, useState } from "react";
import {
  GitBranch,
  LayoutDashboard,
  Loader2,
  Lock,
  LogOut,
  RefreshCw,
  Settings,
  X,
} from "lucide-react";
import { ApiError, api } from "./api/client";
import type {
  AuthConfig,
  CatalogRow,
  Me,
  Product,
  GraphPayload,
  Layer,
  LineageStatus,
  LineageTree,
  Overview,
  ScanPass,
  ScanProgress,
  Subscription,
  TreeNode,
} from "./api/types";
import { Brand } from "./components/Brand";
import { FilterBar } from "./components/FilterBar";
import { ScanProgressStrip } from "./components/ScanProgress";
import { Locked } from "./components/Locked";
import { type Filters, NO_FILTERS, isFiltered } from "./lib/filters";
import { Empty, Loading } from "./components/primitives";
import { DocsOverview } from "./views/DocsOverview";
import { GraphLegend, LineageGraph } from "./views/LineageGraph";
import { ItemPage } from "./views/ItemPage";
import { NodePage, type TabId } from "./views/NodePage";
import { AdminPage, canDocument, isAdmin } from "./views/AdminPage";
import { AuthScreen } from "./views/AuthScreen";
import { planStatusLabel } from "./views/PlanSection";
import { stamp } from "./lib/format";
import { LAYER_LABEL } from "./lib/layers";
import { ITEM_PAGE_KINDS } from "./lib/tree";
import { signInReturn } from "./lib/signInReturn";
import {
  AUDIENCE_NAV,
  AudienceApp,
  type AudienceMeta,
  type AudienceView,
} from "./audience/AudienceApp";

/**
 * Lineage's pages, Settings ("admin", both products), and "audience" — which
 * shows the Audience page named by `audienceView`.
 */
type View = "home" | "dag" | "node" | "item" | "admin" | "audience";

const PRODUCT_KEY = "fabribuddy:product";
/** The Data Catalog page this browser last opened: the overview or the graph. */
const LINEAGE_PAGE_KEY = "fabribuddy:lineage-page";

function initialLineagePage(): "home" | "dag" {
  try {
    return window.localStorage.getItem(LINEAGE_PAGE_KEY) === "dag" ? "dag" : "home";
  } catch {
    return "home";
  }
}

/**
 * Which product opens first: `?product=`, then the address (audience.… opens
 * Audience, lineage.… Lineage), then the last one this browser used.
 */
function initialProduct(): Product {
  const fromUrl = new URLSearchParams(window.location.search).get("product");
  if (fromUrl === "lineage" || fromUrl === "audience") return fromUrl;
  const host = window.location.hostname.toLowerCase();
  if (host.startsWith("audience.")) return "audience";
  if (host.startsWith("lineage.")) return "lineage";
  try {
    const saved = window.localStorage.getItem(PRODUCT_KEY);
    if (saved === "lineage" || saved === "audience") return saved;
  } catch {
    // storage unavailable (private mode): fall through
  }
  return "lineage";
}

const LEDE =
  "Every lakehouse table, what wrote it, what depends on it and who can read it — " +
  "reverse-engineered from the code already in your tenant.";

/**
 * The gate: sign-in first when accounts are on, then the app. A 401 anywhere
 * (expired or revoked session) brings the sign-in screen back.
 */
export default function App() {
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    Promise.all([api.authConfig(), api.me().catch(() => null)])
      .then(([config, user]) => {
        setAuthConfig(config);
        setMe(user);
      })
      .catch(() =>
        setAuthConfig({
          authRequired: false,
          registrationOpen: false,
          emailVerification: false,
        }),
      )
      .finally(() => setChecked(true));
    const signedOut = () => setMe(null);
    window.addEventListener("fl:signed-out", signedOut);
    return () => window.removeEventListener("fl:signed-out", signedOut);
  }, []);

  if (!checked || !authConfig) {
    return (
      <div className="boot">
        <Loader2 className="spin" size={20} aria-hidden="true" />
        <span>Loading FabriBuddy…</span>
      </div>
    );
  }
  // An invitation link opens the accept screen even in a signed-in browser.
  const invited = new URLSearchParams(window.location.search).has("invite");
  if (authConfig.authRequired && (!me || invited)) {
    return <AuthScreen config={authConfig} onSignedIn={setMe} />;
  }
  return (
    <Workspace
      key={me?.id ?? "local"}
      me={me}
      onSignedOut={() => setMe(null)}
      // FL_PUBLIC_DEMO (a demo instance), or the /demo visitor of a normal one.
      // The in-tenant edition is read-only the same way, without the demo's note.
      publicDemo={Boolean(authConfig.publicDemo || me?.demo || authConfig.inTenant)}
      inTenant={Boolean(authConfig.inTenant)}
      signupUrl={authConfig.demoSignupUrl || (me?.demo ? "/?signup" : "")}
    />
  );
}

function Workspace({
  me,
  onSignedOut,
  publicDemo,
  inTenant = false,
  signupUrl,
}: {
  me: Me | null;
  onSignedOut: () => void;
  /** A read-only demo: nothing to scan, no settings, a note saying so. */
  publicDemo: boolean;
  /** The in-tenant edition: read-only, and only the unfiltered estate is stored. */
  inTenant?: boolean;
  signupUrl: string;
}) {
  const [tree, setTree] = useState<LineageTree | null>(null);
  const [status, setStatus] = useState<LineageStatus | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [catalog, setCatalog] = useState<CatalogRow[] | null>(null);
  const [booting, setBooting] = useState(true);
  const [bootError, setBootError] = useState<string | null>(null);

  // Coming back from Stripe Checkout lands on Settings → Plan, which says what
  // happened and watches for the webhook to activate the subscription.
  const fromCheckout = new URLSearchParams(window.location.search).has(
    "billing",
  );
  // Coming back from linking Microsoft lands on Settings → Account.
  const toAccount = signInReturn !== null;
  const [audienceView, setAudienceView] = useState<AudienceView>("overview");
  const [audienceMeta, setAudienceMeta] = useState<AudienceMeta | null>(null);
  // The demo opens on the Data Catalog's lineage graph, whatever this browser
  // last used: the graph is what a visitor came to see.
  const demoLanding =
    publicDemo && !inTenant && !new URLSearchParams(window.location.search).has("product");
  const [product, setProductState] = useState<Product>(() =>
    demoLanding ? "lineage" : initialProduct(),
  );
  const [view, setView] = useState<View>(
    fromCheckout || toAccount
      ? "admin"
      : demoLanding
        ? "dag"
        : initialProduct() === "audience"
          ? "audience"
          : initialLineagePage(),
  );
  /** The overview or the graph, remembered for next time. */
  const openLineagePage = (page: "home" | "dag") => {
    setHighlight(null);
    try {
      window.localStorage.setItem(LINEAGE_PAGE_KEY, page);
    } catch {
      // not persisted; the choice still applies to this session
    }
    setView(page);
  };
  const [nodeId, setNodeId] = useState<string | null>(null);
  const [tab, setTab] = useState<TabId>("overview");

  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [selector, setSelector] = useState("");
  // A lane highlighted in the graph (opened from the overview's flow).
  const [highlight, setHighlight] = useState<Layer | null>(null);
  // One filter state for both views: narrowing to a lakehouse on the Docs
  // page and then opening the graph should show the same lakehouse.
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [scanning, setScanning] = useState(false);

  const [needsTenant, setNeedsTenant] = useState(false);
  const admin = !publicDemo && isAdmin(me);
  // Plan state: drives the trial/expiry banner, the schedule's slot limit and
  // whether the operator's control plane appears at all.
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const refreshSubscription = useCallback(() => {
    api
      .subscription()
      .then(setSubscription)
      .catch(() => setSubscription(null));
  }, []);
  useEffect(() => {
    refreshSubscription();
    // A 402 "locked" from any page means the plan just ended: re-read it.
    window.addEventListener("fl:locked", refreshSubscription);
    return () => window.removeEventListener("fl:locked", refreshSubscription);
  }, [refreshSubscription]);
  // Ended trial or plan: only the two overview pages and Settings open.
  const locked = Boolean(subscription?.locked);
  // A locked organisation lands on the overview even if the graph was last.
  const landed = useRef(false);
  useEffect(() => {
    if (!subscription || landed.current) return;
    landed.current = true;
    if (subscription.locked) setView((v) => (v === "dag" ? "home" : v));
  }, [subscription]);
  // The demo counts which pages its visitors open (the operator's console).
  const demoPage =
    me?.demo && view !== "admin"
      ? view === "audience"
        ? `audience:${audienceView}`
        : `lineage:${view}`
      : null;
  useEffect(() => {
    if (demoPage) api.demoView(demoPage).catch(() => undefined);
  }, [demoPage]);
  // Switching product lands the same way: a locked organisation on the overview.
  const setProduct = (next: Product) => {
    setProductState(next);
    try {
      window.localStorage.setItem(PRODUCT_KEY, next);
    } catch {
      // not persisted; the choice still applies to this session
    }
    const page = initialLineagePage();
    setView(next === "audience" ? "audience" : locked && page === "dag" ? "home" : page);
  };
  const [adminSection, setAdminSection] = useState<"plan" | undefined>(
    undefined,
  );
  const openPlan = () => {
    setAdminSection("plan");
    setView("admin");
  };
  // Bumped whenever a collector finishes, so every view re-reads what it shows.
  const [dataVersion, setDataVersion] = useState(0);

  const load = useCallback(async () => {
    try {
      const [t, s, o] = await Promise.all([
        api.tree(),
        api.status(),
        api.overview(),
      ]);
      setTree(t);
      setStatus(s);
      setOverview(o);
      setNeedsTenant(false);
      setBootError(null);
    } catch (e) {
      // No tenant yet: the Admin page is the onboarding (for an admin; a
      // read-only user is told to wait for one).
      if (e instanceof ApiError && e.status === 409) {
        setNeedsTenant(true);
        if (admin) setView("admin");
        return;
      }
      throw e;
    }
  }, [admin]);

  useEffect(() => {
    load()
      .catch((e) => setBootError(e instanceof ApiError ? e.message : String(e)))
      .finally(() => setBooting(false));
  }, [load]);

  // The graph view is loaded on demand: on a large tenant it is the most
  // expensive read, and most sessions never open it.
  useEffect(() => {
    if (view !== "dag") return;
    let cancelled = false;
    setGraph(null);
    api
      .graph({ select: selector, ...filters })
      .then((g) => !cancelled && setGraph(g))
      .catch(() => !cancelled && setGraph(null));
    return () => {
      cancelled = true;
    };
  }, [view, selector, filters, dataVersion]);

  // The Docs page follows the filters: overview numbers and the catalog.
  useEffect(() => {
    if (needsTenant) return;
    let cancelled = false;
    const scoped = isFiltered(filters) ? filters : undefined;
    api
      .overview(scoped)
      .then((o) => !cancelled && setOverview(o))
      .catch(() => undefined);
    setCatalog(null);
    api
      .catalog(scoped)
      .then((c) => !cancelled && setCatalog(c.rows))
      .catch(() => !cancelled && setCatalog([]));
    return () => {
      cancelled = true;
    };
  }, [filters, needsTenant, dataVersion]);

  const openNode = useCallback((id: string, nextTab: TabId = "overview") => {
    setNodeId(id);
    setTab(nextTab);
    setView("node");
  }, []);
  // The graph on a selector: from the overview (keeping the filters; its rows
  // are already inside them) or from a table page (clearing them, so the
  // table is never filtered out).
  const openGraph = useCallback((next: string, clearFilters = false) => {
    setSelector(next);
    setHighlight(null);
    if (clearFilters) setFilters(NO_FILTERS);
    setView("dag");
  }, []);
  // A graph opened on one node ("View in lineage") names it in a chip.
  const focusId = /^\+id:"?([^"]+?)"?\+$/.exec(selector)?.[1] ?? null;
  const focusNode = focusId ? graph?.nodes.find((n) => n.id === focusId) : undefined;
  // A notebook, pipeline, dataflow… opens its own page (its code and writes).
  const [itemId, setItemId] = useState<string | null>(null);
  const openItem = useCallback((id: string) => {
    setItemId(id);
    setView("item");
  }, []);

  // Bumped to make the poller look now (after starting a scan) instead of at
  // its idle pace.
  const [kick, setKick] = useState(0);
  const finishedRef = useRef<string | null>(null);

  /**
   * One poller for the whole app. A scan runs for minutes and in stages
   * (scanner, items, definitions, parse, delta, access…); the page refreshes
   * as each stage finishes rather than only at the end — and it notices scans
   * it did not start (the schedule, another admin) at the idle pace.
   */
  useEffect(() => {
    if (needsTenant) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const next = await api.status().catch(() => null);
      if (cancelled) return;
      if (next) {
        // What has finished, and when: changes exactly when a stage completes,
        // even one that started and ended between two polls.
        const finished = next.collectors
          .filter((c) => !c.running)
          .map((c) => `${c.kind}@${c.lastRunAt}:${c.lastStatus}`)
          .join("|");
        const changed =
          finishedRef.current !== null && finishedRef.current !== finished;
        finishedRef.current = finished;
        setStatus(next);
        if (!next.anyRunning) setScanning(false);
        if (changed) {
          await load().catch(() => undefined);
          setDataVersion((v) => v + 1);
        }
      }
      timer = setTimeout(tick, next?.anyRunning ? 4000 : 30000);
    };
    timer = setTimeout(tick, kick ? 1500 : 4000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [needsTenant, kick, load]);

  /**
   * Scan progress, for both products and whoever started the scan: every 2s
   * while a pass runs, every 15s otherwise (so a scheduled scan shows up).
   * When a pass ends, everything re-reads, and the strip says how it went.
   */
  const [progress, setProgress] = useState<ScanProgress | null>(null);
  const [finishedPass, setFinishedPass] = useState<ScanPass | null>(null);
  const wasRunning = useRef(false);
  useEffect(() => {
    if (needsTenant) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const next = await api.scanProgress().catch(() => null);
      if (cancelled) return;
      setProgress(next);
      if (next?.running) {
        wasRunning.current = true;
        setFinishedPass(null);
      } else if (wasRunning.current && next) {
        wasRunning.current = false;
        setScanning(false);
        setFinishedPass(next.last);
        await load().catch(() => undefined);
        setDataVersion((v) => v + 1);
      }
      timer = setTimeout(tick, next?.running ? 2000 : 15000);
    };
    timer = setTimeout(tick, kick ? 800 : 0);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [needsTenant, kick, load]);
  // The "finished" note is a moment, not a state.
  useEffect(() => {
    if (!finishedPass) return;
    const timer = setTimeout(() => setFinishedPass(null), 12000);
    return () => clearTimeout(timer);
  }, [finishedPass]);

  const rescan = async () => {
    setScanning(true);
    try {
      await api.scan(false);
      setKick((k) => k + 1);
    } catch (e) {
      setScanning(false);
      setBootError(e instanceof ApiError ? e.message : String(e));
    }
  };

  const busyNow =
    scanning ||
    Boolean(progress?.running) ||
    Boolean(product === "lineage" ? status?.anyRunning : audienceMeta?.busy);

  const lastScan = status?.collectors
    .map((c) => c.lastRunAt)
    .filter((v): v is string => Boolean(v))
    .sort()
    .pop();

  if (booting) {
    return (
      <div className="boot">
        <Loader2 className="spin" size={20} aria-hidden="true" />
        <span>Loading FabriBuddy…</span>
      </div>
    );
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <Brand product={product} />
        <div className="product-switch" role="tablist" aria-label="Product">
          {(["lineage", "audience"] as const).map((p) => (
            <button
              key={p}
              type="button"
              role="tab"
              aria-selected={product === p}
              className={product === p ? "active" : ""}
              onClick={() => setProduct(p)}
            >
              {p === "lineage" ? "Data Catalog" : "Report Audience"}
            </button>
          ))}
        </div>
        <nav className="nav" aria-label="Main">
          {product === "lineage" ? (
            <>
              <button
                type="button"
                className={view === "home" ? "nav-item active" : "nav-item"}
                onClick={() => openLineagePage("home")}
              >
                <LayoutDashboard
                  size={17}
                  strokeWidth={1.8}
                  aria-hidden="true"
                />
                Overview
              </button>
              <button
                type="button"
                className={view === "dag" ? "nav-item active" : "nav-item"}
                onClick={() => openLineagePage("dag")}
              >
                <GitBranch size={17} strokeWidth={1.8} aria-hidden="true" />
                Lineage graph
                {locked && (
                  <Lock size={13} className="nav-lock" aria-label="Locked" />
                )}
              </button>
            </>
          ) : (
            AUDIENCE_NAV.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                type="button"
                className={
                  view === "audience" && audienceView === id
                    ? "nav-item active"
                    : "nav-item"
                }
                onClick={() => {
                  setAudienceView(id);
                  setView("audience");
                }}
              >
                <Icon size={17} strokeWidth={1.8} aria-hidden="true" />
                {label}
                {locked && id !== "overview" && (
                  <Lock size={13} className="nav-lock" aria-label="Locked" />
                )}
              </button>
            ))
          )}
        </nav>
        <div className="sidebar-rule" />
        {product === "lineage" && tree && (
          <TreeHost
            tree={tree}
            selectedId={view === "item" ? itemId : nodeId}
            onSelect={(n) =>
              n.kind === "column" && n.tableId
                ? openNode(n.tableId, "columns")
                : ITEM_PAGE_KINDS.has(n.kind)
                  ? openItem(n.id)
                  : openNode(n.id)
            }
          />
        )}
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="tenant-pill">
            <span className="dot" aria-hidden="true" />
            <span className="name">
              {status?.tenant.displayName ?? "No tenant"}
            </span>
            {status?.tenant.isDemo && <span className="sub">demo data</span>}
          </div>
          <div className="topbar-right">
            <span className="topbar-meta">
              {product === "lineage" ? (
                <>
                  Last scan {stamp(lastScan ?? null)}
                  {tree
                    ? ` · ${tree.counts.workspaces} workspaces · ${tree.counts.items} items`
                    : ""}
                </>
              ) : (
                <>
                  Last scan {stamp(audienceMeta?.lastScan ?? null)}
                  {audienceMeta?.workspaces != null
                    ? ` · ${audienceMeta.workspaces} workspaces · ${audienceMeta.reports} reports`
                    : ""}
                </>
              )}
            </span>
            {admin && (
              <button
                type="button"
                className="btn btn-primary"
                onClick={rescan}
                disabled={busyNow || needsTenant || status?.tenant.isDemo}
                title={
                  status?.tenant.isDemo
                    ? "This is the demo tenant — there is nothing to scan."
                    : "Scans both products — Data Catalog, then Report Audience — re-reading only what changed"
                }
              >
                {busyNow ? (
                  <Loader2 className="spin" size={14} aria-hidden="true" />
                ) : (
                  <RefreshCw size={14} strokeWidth={1.9} aria-hidden="true" />
                )}
                {busyNow ? "Scanning…" : "Rescan"}
              </button>
            )}
            {publicDemo && signupUrl && (
              <a
                className="btn btn-primary"
                href={signupUrl}
                onClick={async (e) => {
                  // The /demo visitor is signed in: leave the demo first, or
                  // the sign-up page would open the demo again.
                  if (!me?.demo) return;
                  e.preventDefault();
                  await api.logout().catch(() => undefined);
                  window.location.assign(signupUrl);
                }}
              >
                Start a free trial
              </a>
            )}
            {!publicDemo && (
              <button
                type="button"
                className={
                  view === "admin"
                    ? "btn btn-icon topbar-settings active"
                    : "btn btn-icon topbar-settings"
                }
                aria-label={admin ? "Settings" : "Account"}
                title={
                  admin
                    ? "Settings — connection, workspaces, scans, plans, members (both products)"
                    : "Account"
                }
                aria-pressed={view === "admin"}
                onClick={() =>
                  setView((v) =>
                    v === "admin"
                      ? product === "audience"
                        ? "audience"
                        : "home"
                      : "admin",
                  )
                }
              >
                <Settings size={16} strokeWidth={1.9} aria-hidden="true" />
              </button>
            )}
            {me?.demo && (
              <button
                type="button"
                className="btn"
                onClick={async () => {
                  await api.logout().catch(() => undefined);
                  onSignedOut();
                }}
              >
                Exit demo
              </button>
            )}
            {me && !me.demo && (
              <div className="user-chip" title={me.email}>
                <span className="user-name">{me.displayName || me.email}</span>
                <button
                  type="button"
                  className="btn btn-icon"
                  aria-label="Sign out"
                  onClick={async () => {
                    await api.logout().catch(() => undefined);
                    onSignedOut();
                  }}
                >
                  <LogOut size={14} aria-hidden="true" />
                </button>
              </div>
            )}
          </div>
        </header>
        <ScanProgressStrip progress={progress} finished={finishedPass} />

        <main className="content">
          {publicDemo && !inTenant && (
            <p className="banner banner-demo">
              <strong>Live demo.</strong> A fictional Contoso tenant, scanned by
              the real product. Look around freely — nothing here can be
              changed.
            </p>
          )}
          {bootError && view !== "admin" && (
            <p className="banner banner-danger">{bootError}</p>
          )}

          {subscription && !publicDemo && (
            <PlanBanner
              subscription={subscription}
              admin={admin}
              onOpen={openPlan}
            />
          )}

          {view === "admin" && (
            <AdminPage
              me={me}
              onSignedOut={onSignedOut}
              onTenantChanged={() => {
                load().catch(() => undefined);
                setDataVersion((v) => v + 1);
                setKick((k) => k + 1);
              }}
              key={adminSection ?? "settings"}
              initialSection={
                fromCheckout || adminSection === "plan"
                  ? "plan"
                  : toAccount
                    ? "account"
                    : needsTenant
                      ? "prerequisites"
                      : undefined
              }
              subscription={subscription}
              onSubscriptionChanged={setSubscription}
            />
          )}

          {(view === "home" || view === "dag") && tree && !inTenant && (
            <FilterBar tree={tree} filters={filters} onChange={setFilters} />
          )}

          {view === "home" &&
            (overview ? (
              <DocsOverview
                overview={overview}
                catalog={catalog}
                lede={LEDE}
                exportUrl={
                  inTenant
                    ? undefined
                    : api.catalogCsvUrl(isFiltered(filters) ? filters : undefined)
                }
                onPickNode={(id, nextTab) =>
                  openNode(id, (nextTab as TabId) ?? "overview")
                }
                onOpenGraph={openGraph}
              />
            ) : (
              <Empty title="No data yet">
                {admin
                  ? "Connect a tenant and run a scan, or start the backend with FL_DEMO_MODE=1."
                  : "Your organisation's admins have not connected a Fabric tenant yet. " +
                    "The Data Catalog appears here after their first scan."}
              </Empty>
            ))}

          {locked && (view === "dag" || view === "node") && (
            <Locked
              page={view === "dag" ? "The lineage graph" : "The table page"}
              admin={admin}
              trial={subscription?.planCode === "trial"}
              onChoosePlan={openPlan}
            />
          )}

          {view === "dag" && !locked && (
            <div className="stack">
              <div
                className="toolbar"
                style={{ justifyContent: "space-between" }}
              >
                <div className="page-head">
                  <h1 className="page-title">Lineage graph</h1>
                  <p className="page-lede">
                    Every item across all workspaces and how data flows between
                    them. Click a table to trace its lineage; drag it to
                    rearrange.
                  </p>
                </div>
                <GraphLegend />
              </div>

              <div className="toolbar">
                <label htmlFor="sel" style={{ fontSize: 13, fontWeight: 600 }}>
                  Select
                </label>
                {focusId ? (
                  // Opened on one table, model or report: name it, not its id.
                  <button
                    type="button"
                    className="focus-chip"
                    title={focusNode?.path}
                    onClick={() => setSelector("")}
                  >
                    Lineage of {focusNode ? focusNode.shortName || focusNode.name : "…"}
                    {focusNode && (
                      <span className="focus-chip-kind">{focusNode.kindLabel}</span>
                    )}
                    <X size={13} aria-label="Show everything" />
                  </button>
                ) : (
                  <input
                    // Re-read when the selector is set from elsewhere (the
                    // overview's flow, a table's "Open in graph").
                    key={selector}
                    id="sel"
                    className="input"
                    style={{ maxWidth: 340, background: "var(--card)" }}
                    placeholder="+fact_sales+ · layer:warehouse"
                    defaultValue={selector}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") setSelector(e.currentTarget.value);
                    }}
                    onBlur={(e) => setSelector(e.currentTarget.value)}
                  />
                )}
                {highlight && (
                  <button
                    type="button"
                    className="focus-chip"
                    onClick={() => setHighlight(null)}
                  >
                    Highlighted: {LAYER_LABEL[highlight]}
                    <X size={13} aria-label="Clear" />
                  </button>
                )}
                <span className="muted-note">
                  {focusId
                    ? "What feeds it and what it feeds. Clear it to see everything."
                    : "+table+ shows what feeds it and what it feeds. Enter to apply, empty for everything."}
                </span>
              </div>

              {graph ? (
                <LineageGraph
                  payload={graph}
                  onPick={(id) => openNode(id)}
                  highlightLayer={highlight}
                  focusId={focusId}
                  // An arrangement belongs to the graph it was made in.
                  layoutKey={JSON.stringify({ selector, filters })}
                />
              ) : (
                <Loading what="the graph" />
              )}
            </div>
          )}

          {view === "audience" && locked && audienceView !== "overview" && (
            <Locked
              page={
                AUDIENCE_NAV.find((n) => n.id === audienceView)?.label ??
                "This page"
              }
              admin={admin}
              trial={subscription?.planCode === "trial"}
              onChoosePlan={openPlan}
            />
          )}

          {view === "audience" && (!locked || audienceView === "overview") && (
            <AudienceApp
              view={audienceView}
              onNavigate={setAudienceView}
              admin={admin}
              kick={kick}
              onMeta={setAudienceMeta}
              onRescan={rescan}
            />
          )}

          {view === "item" && itemId && !locked && (
            <ItemPage
              key={`${itemId}@${dataVersion}`}
              itemId={itemId}
              onPickNode={(id) => openNode(id)}
              onPickItem={openItem}
            />
          )}

          {view === "node" && nodeId && !locked && (
            <NodePage
              // Remount after a stage finishes so the page shows fresh data.
              key={`${nodeId}@${dataVersion}`}
              nodeId={nodeId}
              tab={tab}
              onTab={setTab}
              onPickNode={(id, nextTab) => openNode(id, nextTab ?? "overview")}
              onOpenGraph={(next) => openGraph(next, true)}
              canEdit={!publicDemo && canDocument(me)}
            />
          )}
        </main>
      </div>
    </div>
  );
}

/**
 * The tree is lazily imported so the sidebar's search state survives view
 * changes — remounting it on every navigation would clear the query someone
 * just typed.
 */
function TreeHost({
  tree,
  selectedId,
  onSelect,
}: {
  tree: LineageTree;
  selectedId: string | null;
  onSelect: (node: TreeNode) => void;
}) {
  const [Loaded, setLoaded] = useState<React.ComponentType<{
    tree: LineageTree;
    selectedId: string | null;
    onSelect: (node: TreeNode) => void;
  }> | null>(null);

  useEffect(() => {
    import("./views/TreeSidebar").then((m) => setLoaded(() => m.TreeSidebar));
  }, []);

  if (!Loaded) return <div className="tree-empty">Loading tree…</div>;
  return <Loaded tree={tree} selectedId={selectedId} onSelect={onSelect} />;
}

/**
 * The one plan nudge: shown only when there is something to act on — a trial
 * in its last week, or a lapsed subscription.
 */
function PlanBanner({
  subscription: sub,
  admin,
  onOpen,
}: {
  subscription: Subscription;
  admin: boolean;
  onOpen: () => void;
}) {
  const action = admin ? (
    <button type="button" className="link" onClick={onOpen}>
      Choose a plan
    </button>
  ) : (
    <span>Ask an admin to choose a plan.</span>
  );
  if (!sub.active) {
    return (
      <p className="banner banner-warn">
        {sub.planCode === "trial"
          ? "Your trial has ended: the tenant connection and everything collected from it have been deleted. Choose a plan, then reconnect and scan to start again."
          : `Your ${sub.planName} plan has ended. The overview pages stay open and everything collected is kept; the rest is locked and scans are paused.`}{" "}
        {action}
      </p>
    );
  }
  if (
    sub.status === "trialing" &&
    sub.trialDaysLeft !== null &&
    sub.trialDaysLeft <= 7
  ) {
    return (
      <p className="banner banner-warn">
        {planStatusLabel(sub)} — the trial covers {sub.limits.workspaces ?? "all"}{" "}
        workspaces. <strong>When it ends, the tenant connection and everything collected are
        deleted</strong> unless you choose a plan. {action}
      </p>
    );
  }
  return null;
}
