/** Shapes returned by the FabriBuddy backend. camelCase, no mapping layer. */

export type Layer =
  | "source"
  | "mirrored"
  | "lakehouse"
  | "warehouse"
  | "semantic"
  | "report";

export type NodeKind =
  | "lakehouse_table"
  | "warehouse_table"
  | "mirrored_table"
  | "external"
  | "model_table"
  | "report_visual";

export type Tone = "plain" | "warn" | "danger";

/* ------------------------------------------------------------------- tree */

export interface TreeNode {
  id: string;
  /** Node kinds plus structural levels: workspace, itemtype, schema, group, connection. */
  kind: string;
  /** On an `itemtype` folder: the Fabric item type it groups. */
  itemType?: string;
  name: string;
  count: number;
  children?: TreeNode[];
  layer?: Layer;
  hasContributor?: boolean;
  sensitivityLabel?: string;
  endorsement?: string;
  /** On a storage table: its columns as [name, data type], in table order. */
  columns?: [string, string][];
  /** On a column (unfolded by the sidebar): its type, and the table it belongs to. */
  dataType?: string;
  tableId?: string;
}

export interface LineageTree {
  workspaces: TreeNode[];
  external: TreeNode;
  counts: { workspaces: number; items: number; tables: number; external: number };
}

/* --------------------------------------------------------------- overview */

export interface Stat {
  key: string;
  value: string | number;
  tone: Tone;
}

export interface Finding {
  type: string;
  severity: "critical" | "serious" | "warning";
  /** Short label for the chip: Stale, PII, Docs, Access, Orphan, Lineage. */
  tone: string;
  title: string;
  detail: string;
  nodeId: string | null;
  itemId: string | null;
  /** Which node tab answers this finding. */
  tab: string;
}

export interface RecentWrite {
  nodeId: string;
  name: string;
  by: string;
  operation: string;
  committedAt: string | null;
}

/** One lane of the estate, source to report: tables, or models and reports as items. */
export interface FlowLane {
  layer: Layer;
  label: string;
  count: number;
  /** Lakehouses, warehouses or mirrored databases holding the lane's tables. */
  containers: number;
}

/** What `from` feeds in `to`, counted in the `to` lane's unit. */
export interface FlowLink {
  from: Layer;
  to: Layer;
  count: number;
}

export interface Overview {
  stats: Stat[];
  findings: Finding[];
  recent: RecentWrite[];
  flow: { lanes: FlowLane[]; links: FlowLink[] };
  /** What the last scans could not read (Collection gaps). */
  gaps: number;
}

/* ---------------------------------------------------------------- catalog */

export interface CatalogRow {
  id: string;
  name: string;
  schema: string;
  kind: NodeKind;
  kindLabel: string;
  layer: Layer;
  workspaceId: string;
  workspace: string;
  itemId: string;
  item: string;
  itemType: string;
  producers: { item: string; kind: string; mode: string }[];
  /** One category per row, for the charts: Notebook, Pipeline, Dataflow Gen2, … */
  producerKind: string;
  /**
   * "code": an item's code names this table as its target. "delta": no item
   * does, and the kind is the engine the Delta log says wrote it (the writer
   * itself could not be named). "": nothing is known.
   */
  producerEvidence?: "code" | "delta" | "";
  upstream: number;
  models: number;
  reports: number;
  lastWriteAt: string | null;
  rows: number | null;
  sizeBytes: number | null;
  documented: boolean;
  /** Columns, and how many have a description. */
  columns: number;
  columnsDocumented: number;
  /** A column is tagged personal data. */
  pii: boolean;
  /** A semantic model or report: what it is built on, by name. */
  sources: string[];
  /** A semantic model's storage mode: DirectLake, Import… */
  storageMode: string;
}

export interface Catalog {
  rows: CatalogRow[];
}

/* ------------------------------------------------------------------ graph */

export interface NodeBrief {
  id: string;
  name: string;
  shortName: string;
  sub: string;
  layer: Layer;
  layerLabel: string;
  kind: NodeKind;
  kindLabel: string;
  path: string;
  /** The workspace it sits in; empty for an external source. */
  workspace?: string;
}

export interface GraphNode extends NodeBrief {
  isFocus: boolean;
  onPath: boolean;
}

export interface GraphEdge {
  from: string;
  to: string;
  viaItem: string;
  viaRef: string;
  writeMode: string;
  source: "parsed" | "runtime" | "declared" | "inferred";
  confidence: "low" | "medium" | "high";
  isDynamic: boolean;
  onPath: boolean;
}

export interface Lane {
  layer: Layer;
  label: string;
  nodeIds: string[];
}

export interface GraphPayload {
  focus: string | null;
  lanes: Lane[];
  nodes: GraphNode[];
  edges: GraphEdge[];
}

/* -------------------------------------------------------------- node page */

export interface Tag {
  label: string;
  tone: "danger" | "warn" | "neutral";
}

export interface Detail {
  key: string;
  value: string;
}

/** Where a description came from; for one written in FabriBuddy, who and when. */
export interface DescriptionMeta {
  source: "user" | "tmdl" | "delta_property" | string;
  sourceLabel?: string;
  updatedBy: string;
  updatedAt: string | null;
}

export interface NodePage extends NodeBrief, FabricLink {
  description: string;
  hasDescription: boolean;
  descriptionMeta?: DescriptionMeta | null;
  tags: Tag[];
  details: Detail[];
  upstream: NodeBrief[];
  downstream: NodeBrief[];
  /** False when the lane was inferred from graph depth rather than a name rule. */
  layerIsDeclared: boolean;
}

/** "Open in Fabric": the item in the Fabric portal; empty on the demo tenant. */
export interface FabricLink {
  fabricUrl: string;
  /** The demo tenant's ids are invented, so there is nothing to open. */
  fabricDemo: boolean;
}

export interface Column {
  id: string;
  name: string;
  type: string;
  nullable: boolean;
  description: string;
  hasDescription: boolean;
  descriptionMeta?: DescriptionMeta | null;
  tags: string[];
  isPii: boolean;
  isMeasure: boolean;
}

export interface CodePayload {
  available: boolean;
  reason?: string;
  definedIn?: string;
  item?: string;
  itemType?: string;
  partPath?: string;
  language?: string;
  detectedVia?: string;
  text?: string;
  /** The block (cell / query / activity) and, within it, the lines that write. */
  highlight?: { startLine: number; endLine: number; lines?: number[] } | null;
  writeMode?: string;
  fetchedAt?: string | null;
  fetchMode?: string;
  /** One entry per producing item when more than one writes this table. */
  definitions?: CodePayload[];
}

export interface AccessRow {
  principal: string;
  principalType: string;
  upn: string;
  path: string;
  permission: string;
  isIndirect: boolean;
  note: string;
}

export interface AccessPayload {
  summary: string;
  rows: AccessRow[];
}

/* ------------------------------------------------------------ column trace */

export interface TraceItem {
  columnId: string;
  nodeId: string;
  table: string;
  column: string;
  transform: string;
  isPii: boolean;
  isSelected: boolean;
}

export interface TraceLane {
  layer: Layer;
  label: string;
  items: TraceItem[];
}

export interface TracePayload {
  columnId: string;
  column: string;
  nodeId: string;
  table: string;
  lanes: TraceLane[];
  summary: string;
  note: string;
}

/* ----------------------------------------------------------------- search */

export interface SearchPayload {
  nodes: NodeBrief[];
  columns: {
    columnId: string;
    nodeId: string;
    name: string;
    table: string;
    description: string;
    isPii: boolean;
  }[];
  code: {
    itemId: string;
    item: string;
    itemType: string;
    partPath: string;
    snippet: string;
  }[];
}

/* ---------------------------------------------------------------- status */

export interface Capability {
  available: boolean;
  reason?: string;
}

export interface CollectorStatus {
  kind: string;
  running: boolean;
  startedAt: string | null;
  lastRunAt: string | null;
  lastStatus: string | null;
  lastError: string | null;
  stats: Record<string, unknown>;
}

export interface LineageStatus {
  tenant: {
    id: string;
    displayName: string;
    isDemo: boolean;
    capabilities: Record<string, Capability>;
  };
  collectors: CollectorStatus[];
  anyRunning: boolean;
}

export interface CollectorError {
  collector: string;
  scope: string;
  scopeId: string;
  scopeName: string;
  reason:
    | "needs_contributor"
    | "protected_label"
    | "rate_limited"
    | "unsupported"
    | "error";
  message: string;
  occurredAt: string | null;
}

export interface Tenant {
  id: string;
  displayName: string;
  tenantId: string;
  clientId: string;
  status: "connected" | "unvalidated" | "error";
  capabilities: Record<string, Capability>;
  contributorWorkspaces: string[];
  isDemo: boolean;
  secretStore: string;
  lastValidatedAt: string | null;
  lastValidationError: string | null;
  createdAt: string | null;
}

/* ------------------------------------------------------------- accounts */

export interface AuthConfig {
  authRequired: boolean;
  registrationOpen: boolean;
  emailVerification: boolean;
  /** FL_PUBLIC_DEMO: a read-only demo — no scans, no settings. */
  publicDemo?: boolean;
  /** Where the demo's "Start a free trial" goes. */
  demoSignupUrl?: string;
  /** "Continue with Microsoft" is offered (FL_MS_CLIENT_ID is set). */
  microsoftSignIn?: boolean;
  /**
   * The in-tenant edition (a Fabric App over what its notebook stored): read
   * only, signed in by Fabric, scanned by a pipeline — no settings, no scan
   * button, no plans, no filters beyond the catalog's own search.
   */
  inTenant?: boolean;
}

export interface Me {
  id: string;
  email: string;
  displayName: string;
  role: string;
  emailVerified: boolean;
  createdAt: string | null;
  /** Signs in with a Microsoft work account. */
  microsoft?: boolean;
  /** False for an account that has only ever signed in with Microsoft. */
  hasPassword?: boolean;
  /** The shared visitor of the public demo (app.fabribuddy.com/demo). */
  demo?: boolean;
}

/* ---------------------------------------------------------------- admin */

export type ScopeMode = "all" | "selected";

export interface ScheduleIn {
  enabled: boolean;
  times: string[];
  timezone: string;
  fullWeekday: number | null;
}

export interface AdminTenant {
  connected: boolean;
  id?: string;
  displayName?: string;
  tenantId?: string;
  clientId?: string;
  secretSet?: boolean;
  status?: string;
  capabilities?: Record<string, { available: boolean; reason?: string }>;
  lastValidatedAt?: string | null;
  lastValidationError?: string | null;
  scope?: { mode: ScopeMode; workspaceIds: string[]; effective: number };
  schedule?: ScheduleIn & { lastScheduledAt: string | null };
}

/** Admin: settings and scans. Contributor: writes descriptions. Viewer: read-only. */
export type Role = "admin" | "contributor" | "viewer";

export interface Member {
  id: string;
  email: string;
  displayName: string;
  role: Role;
  isYou: boolean;
  createdAt: string | null;
  lastLoginAt: string | null;
  lastSeenAt: string | null;
}

export interface PendingInvitation {
  id: string;
  email: string;
  role: Role;
  createdAt: string | null;
  expiresAt: string;
}

export interface CreatedInvitation extends PendingInvitation {
  link: string;
  emailSent: boolean;
}

export interface MembersPayload {
  members: Member[];
  invitations: PendingInvitation[];
  adminSeats: { used: number; max: number };
  emailEnabled: boolean;
}

export interface AdminWorkspace {
  id: string;
  name: string;
  type: string;
  /** The SPN's workspace role; "none" = only seen by Audience's admin-API inventory. */
  access: "contributor" | "viewer" | "none";
  selected: boolean;
  /** Unselected, but code in the selection reads or writes it (item names). */
  referencedBy?: string[];
  /** Which products can scan it. */
  lineage: boolean;
  audience: boolean;
  reports?: number;
  models?: number;
}

export interface AdminWorkspaces {
  mode: ScopeMode;
  workspaces: AdminWorkspace[];
  includePersonal: boolean;
  /** Report Audience's inventory has run (its rows are known). */
  scanned: boolean;
  /** The plan's workspace cap, shared by both products; null = unlimited. */
  cap: number | null;
  fabricError: string | null;
}

/* --------------------------------------------------------- plan & billing */

export type BillingPeriod = "monthly";
/** The two FabriBuddy products in this app (Data Catalog, Report Audience). */
export type Product = "lineage" | "audience";
/** One subscription covers both products; plans differ by workspaces scanned. */
export type PlanCode = "trial" | "team" | "business" | "enterprise";
export type SubscriptionStatus = "trialing" | "active" | "past_due" | "canceled";

/** GET /auth/subscription — the organisation's one plan against its usage. */
export interface Subscription {
  planCode: PlanCode;
  planName: string;
  /** In `currency`, excluding VAT. */
  priceMonth: number;
  currency: string;
  status: SubscriptionStatus;
  /** False once a trial or paid period has lapsed. */
  active: boolean;
  /** Ended: only the overview pages and Settings open until a plan is chosen. */
  locked?: boolean;
  trialDaysLeft: number | null;
  expiresAt: string | null;
  daysLeft: number | null;
  billingPeriod: BillingPeriod;
  /** null = unlimited. */
  limits: {
    users: number | null;
    workspaces: number | null;
    tenants: number;
    dailyScans: number;
  };
  products: { product: Product; name: string }[];
  organisation: string;
  usage: { users: number; invitations: number; workspaces: number; tenants: number };
  /** The smallest paid plan that covers the current selection. */
  suggestedPlan: PlanCode;
  over: ("users" | "workspaces" | "tenants")[];
  isPlatformAdmin: boolean;
  /** Who takes payment: Paddle (merchant of record), Stripe, or manual invoicing. */
  billingProvider?: "paddle" | "stripe" | "manual";
  /** A provider is configured: a plan choice goes through its checkout. */
  billingEnabled: boolean;
  hasBillingAccount: boolean;
}

/** GET /auth/plans — the tiers, what both products include, and the trial. */
export interface PlanCatalogue {
  products: { product: Product; name: string; included: string[] }[];
  common: string[];
  /** ISO code of every price; prices exclude VAT (`vatIncluded` false). */
  currency: string;
  vatIncluded: boolean;
  trial: { days: number; maxWorkspaces: number };
  plans: {
    code: PlanCode;
    name: string;
    blurb: string;
    /** In the catalogue's currency, excluding VAT. */
    priceMonth: number;
    /** null = unlimited. */
    maxWorkspaces: number | null;
  }[];
}

/* ----------------------------------------------------------- scan passes */

/** One scan pass: both products, one after the other. */
export interface ScanPass {
  id: string;
  trigger: "manual" | "schedule" | "api";
  incremental: boolean;
  products: Product[];
  status: "running" | "ok" | "partial" | "failed";
  startedAt: string | null;
  finishedAt: string | null;
  durationSeconds: number | null;
  stepsTotal: number;
  stepsDone: number;
  stepsFailed: number;
  outcome: Record<
    string,
    {
      status?: string;
      steps?: Record<string, { seconds?: number; status?: string; error?: string }>;
    }
  >;
}

/** GET /scan/progress — what the running pass is reading, and how far along. */
export interface ScanProgress {
  running: boolean;
  pass: {
    passId: string;
    startedAt: string;
    stepsTotal: number;
    stepsDone: number;
    product?: Product | null;
    step?: string | null;
    label: string;
    /** Sub-progress inside the step (definitions 45 of 200), when it reports one. */
    done?: number | null;
    total?: number | null;
    detail?: string;
    percent: number;
    stepSeconds?: number;
    /** Each product's own progress, in pass order. */
    products?: {
      product: Product;
      steps: number;
      done: number;
      state: "done" | "running" | "waiting";
    }[];
  } | null;
  last: ScanPass | null;
}

/* ---------------------------------------------------------- control plane */

export interface PlatformSubscription {
  planCode: PlanCode;
  planName: string;
  status: SubscriptionStatus;
  active: boolean;
  expiresAt: string | null;
  daysLeft: number | null;
  monthlyValue: number;
  maxWorkspaces: number | null;
  expiringSoon: boolean;
  expired: boolean;
}

export interface PlatformCustomer {
  id: string;
  name: string;
  subscription: PlatformSubscription;
  monthlyValue: number;
  expiringSoon: boolean;
  /** The subscription ended (or never started). */
  expired: boolean;
  createdAt: string | null;
  users: number;
  invitations: number;
  workspaces: number;
  tenants: number;
  tenantNames: string[];
  nodes: number;
  activityEvents: number;
  failedRuns7d: number;
  lastLoginAt: string | null;
  /** Last authenticated request by anyone in the organisation. */
  lastSeenAt: string | null;
  activeUsers7d: number;
  activeUsers30d: number;
  scans30d: number;
  lastScanAt: string | null;
  /** How far through Settings → Prerequisites (Graph is optional, not counted). */
  setup: {
    done: number;
    total: number;
    steps: { key: string; label: string; done: boolean; optional?: boolean }[];
  };
  /** First-touch source label ("Google Ads", "LinkedIn", "Direct"…) and its raw tags. */
  source: string;
  sourceDetail: Record<string, string>;
  /** Any scan ever ran, and how the latest pass ended. */
  scanned: boolean;
  lastPassStatus: string | null;
  isPlatformAdmin: boolean;
  notes: string;
  dormant: boolean;
}

export interface PlatformOverview {
  customers: PlatformCustomer[];
  totals: {
    customers: number;
    paying: number;
    trialing: number;
    users: number;
    workspaces: number;
    nodes: number;
    activeUsers7d: number;
    activeUsers30d: number;
    mrr: number;
    arr: number;
    currency: string;
    byPlan: Record<string, { name: string; paying: number; mrr: number }>;
    expiringSoon: number;
    dormant: number;
    failing: number;
  };
}

/** GET /platform/scans — every pass across customers. */
export interface PlatformScans {
  scans: (ScanPass & { customerId: string; customerName: string })[];
  summary: {
    count: number;
    running: number;
    failed: number;
    partial: number;
    medianSeconds: number | null;
  };
}

export interface PlatformUser {
  id: string;
  email: string;
  displayName: string;
  role: string;
  isActive: boolean;
  createdAt: string | null;
  lastLoginAt: string | null;
  lastSeenAt: string | null;
}

/** The console's Infrastructure view: how the app server, scans and database cope. */
export interface InfraMetric {
  now: number | null;
  peak1h: number | null;
  avg24h: number | null;
  peak24h: number | null;
  series: number[];
}
export interface InfraSnapshot {
  server: {
    sku: string;
    instance: string;
    cpuCount: number;
    cpuPercent: number;
    load: number[] | null;
    memoryTotalMb: number;
    memoryUsedMb: number;
    memoryPercent: number;
    appMemoryMb: number;
    uptimeSeconds: number;
  };
  scans: {
    workers: number;
    running: number;
    last24h: number;
    failed24h: number;
    averageSeconds: number;
    p95Seconds: number | null;
    workerBusyPercent: number;
    scheduledPerDay: number;
    scheduledHoursPerDay: number;
    capacityHoursPerDay: number;
    activeTrials: number;
  };
  database: {
    engine: string;
    sizeMb: number;
    limitMb: number;
    percent: number | null;
    tables: { name: string; sizeMb: number; rows: number }[];
  };
  azure:
    | null
    | { error: string }
    | {
        plan?: Record<string, InfraMetric> | { error: string };
        database?: Record<string, InfraMetric> | { error: string };
      };
  measuredAt: string;
}

/** The console's Website tab: fabribuddy.com traffic and what it became. */
export interface TrafficRow {
  label: string;
  visits: number;
  pageViews: number;
}
export interface TrafficReport {
  days: number;
  since: string;
  until: string;
  configured: boolean;
  website:
    | null
    | { error: string }
    | {
        visits: number;
        pageViews: number;
        daily: TrafficRow[];
        pages: TrafficRow[];
        referrers: TrafficRow[];
        countries: TrafficRow[];
        devices: TrafficRow[];
      };
  app: {
    signups: number;
    connected: number;
    scanned: number;
    paying: number;
    signupsByDay: Record<string, number>;
    bySource: Record<string, { signups: number; connected: number; scanned: number; paying: number }>;
  };
}

/** A code item (notebook, pipeline, dataflow…) on its own page. */
export interface ItemLink {
  id: string;
  name: string;
  kind: string;
  mode: string;
  confidence: string;
  where: string;
}
export interface ItemPagePayload extends FabricLink {
  id: string;
  name: string;
  type: string;
  typeLabel: string;
  path: string;
  description: string;
  artifacts: { partPath: string; language: string; text: string; fetchedAt: string | null }[];
  codeUnavailable: string;
  /** What the code says, read again now — resolved to a table or not. */
  parsed: { target: string; mode: string; where: string; reads: string[]; resolved: boolean }[];
  writes: ItemLink[];
  reads: ItemLink[];
  warnings: string[];
  /** A Variable Library: its variables and the items that name it. */
  variables: {
    name: string;
    type: string;
    note: string;
    default: string;
    active: string;
    overridden: boolean;
  }[];
  activeValueSet: string;
  usedBy: { id: string; name: string; type: string }[];
  /** A lakehouse or warehouse: the tables it holds and the items writing them. */
  tables: ItemLink[];
  writtenBy: { id: string; name: string; type: string; own: boolean }[];
  /** A lakehouse, warehouse, mirrored or SQL database: it holds tables. */
  storage: boolean;
}

/* --------------------------------------------------------- console: demo */

export interface DemoDay {
  label: string;
  visits: number;
  /** Visits that opened at least one page (not a link preview). */
  engaged: number;
}
export interface DemoReport {
  since: string;
  until: string;
  visits: number;
  engaged: number;
  returns: number;
  pageViews: number;
  pagesPerVisit: number;
  bothProducts: number;
  daily: DemoDay[];
  sources: { label: string; visits: number }[];
  pages: { label: string; visits: number }[];
}

/* -------------------------------------------------------- console: costs */

export type CostReport =
  | { error: string }
  | {
      scope: string;
      currency: string;
      monthToDate: number;
      projected: number;
      lastMonth: number;
      daily: { date: string; cost: number }[];
      byService: { name: string; cost: number }[];
      budget: { name: string; amount: number; timeGrain: string } | null;
      fetchedAt: string;
    };
