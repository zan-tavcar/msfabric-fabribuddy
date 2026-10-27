/*
 * API shapes. Audience data (inventory, activity, licences, permissions) is
 * served as the scanners produced it; the platform shapes (accounts, admin,
 * plans, control plane) match FabriBuddy's.
 */

/** Shapes returned by the FabriBuddy backend. */

export interface Tenant {
  id: string;
  displayName: string;
  tenantId: string;
  clientId: string;
  status: "connected" | "unvalidated" | "error";
  capabilities: Record<string, { available: boolean; reason?: string }>;
  secretStore: string;
  refreshIntervalHours: number;
  activityWindowDays: number;
  lastValidatedAt: string | null;
  lastValidationError: string | null;
  createdAt: string | null;
}

export interface Summary {
  workspaces: number;
  sharedWorkspaces: number;
  personalWorkspaces: number;
  reports: number;
  models: number;
  users: number;
}

export interface Workspace {
  id: string;
  name: string;
  type: string;
  state: string;
  isPersonal: boolean;
  /** Fabric / PPU / Premium / Pro / Personal, from the capacity SKU. */
  kind?: string;
  capacityId?: string;
  onDedicatedCapacity?: boolean;
  models: string[];
  reports: string[];
  users: string[];
  roles: Record<string, string>;
}

export interface Report {
  id: string;
  name: string;
  ws: string;
  wsType: string;
  datasetId: string;
  webUrl: string;
  model: string;
}

export interface SemanticModel {
  id: string;
  name: string;
  ws: string;
  wsType: string;
  configuredBy: string;
  isRefreshable: boolean | null;
}

export interface Principal {
  name: string;
  identifier: string;
  principalType: string;
  /** [workspace name, role] pairs */
  workspaces: [string, string][];
  models: string[];
  reports: string[];
}

export interface ActivityEvent {
  ts: string;
  user: string;
  action: string;
  report: string;
  ws: string;
}

/**
 * Aggregates are computed server-side over EVERY event in the window; `events`
 * is only a bounded sample for the feed. Anything counting activity must use
 * the aggregates, never the sample.
 */
export interface Activity {
  live: boolean;
  error?: string;
  /** False when no activity scan has ever been stored. */
  scanned?: boolean;
  /** True while a scan is in flight. */
  running?: boolean;
  /** Stored window narrower than the one requested. */
  stale?: boolean;
  events: ActivityEvent[];
  /** What the figures cover: the scanned workspaces, or the whole tenant. */
  scope?: { scoped: boolean; workspaces: string[]; viewsElsewhere: number };
  summary: {
    total: number;
    views: number;
    exports: number;
    distinctUsers: number;
    viewerUsers?: number;
    days: number;
    truncated?: boolean;
    sampled?: boolean;
    daysRequested?: number;
    /** True when the window stopped early (retention or a network fault). */
    partial?: boolean;
    stoppedReason?: string;
    humanEvents?: number;
    humanUsers?: number;
  };
  topReports: { report: string; views: number }[];
  topUsers?: { user: string; views: number }[];
  filter?: string;
  actionMix?: { action: string; count: number }[];
  activityTypes?: string[];
  daily?: {
    date: string; views: number; other: number;
    users?: number; viewers?: number;
  }[];
  activeUsers?: string[];
  touchedReports?: string[];
  touchedWorkspaces?: string[];
  eventsByUser?: Record<string, number>;
  exportsByUser?: Record<string, number>;
  lastSeenByUser?: Record<string, string>;
  eventsByWorkspace?: Record<string, number>;
  lastSeenByWorkspace?: Record<string, string>;
  lastSeenByReport?: Record<string, string>;
  lastSeenByItem?: Record<string, string>;
  lastRefreshByDataset?: Record<string, string>;
  humanUserList?: string[];
}

export interface LicenseRow {
  name: string;
  upn?: string;
  /** Any activity, not just report views. */
  lastSeen?: string;
  license: "Pro" | "PPU";
  unused: boolean;
  activityKnown: boolean;
}

export interface Licenses {
  live: boolean;
  error?: string;
  rows: LicenseRow[];
  summary: {
    pro: number;
    ppu: number;
    total: number;
    unused: number;
    activityJoined: boolean;
  };
}

export interface ScanRun {
  id: string;
  kind: string;
  status: string;
  error: string | null;
  params: Record<string, unknown>;
  startedAt: string | null;
  finishedAt: string | null;
}

/* --- per-item permissions (admin scanner API) ---------------------------- */

export interface PermPrincipal {
  name: string;
  upn: string;
  graphId: string;
  type: string;
  right: string;
}
/** A grant is a principal plus the right they hold on one item. */
export type PermGrant = PermPrincipal;

export interface PermWorkspace {
  id: string;
  name: string;
  type: string;
  state: string;
  users: PermGrant[];
}

export interface PermReport {
  id: string;
  name: string;
  wsId: string;
  wsName: string;
  datasetId: string;
  createdBy: string;
  modifiedDateTime?: string;
  reportType?: string;
  endorsement?: string;
  sensitivityLabel?: string;
  users: PermGrant[];
  /** A "People in your organization" sharing link: anyone in the tenant with it. */
  orgLink?: { right: string; sharer: string } | null;
  /** Published to web: public on the internet, no sign-in. */
  publishedToWeb?: { sharer: string } | null;
}

export interface PermDataset {
  id: string;
  name: string;
  wsId: string;
  wsName: string;
  configuredBy: string;
  /** Raw targetStorageMode from the scanner API: Import / DirectQuery / Direct Lake. */
  storageMode?: string;
  contentProvider?: string;
  endorsement?: string;
  createdDate?: string;
  users: PermGrant[];
}

export interface RefreshableItem {
  name: string;
  status: string;
  endTime: string;
  refreshType: string;
  refreshCount: number | null;
  refreshFailures: number | null;
  scheduled: boolean;
  workspace: string;
  capacity: string;
}

export interface Refreshables {
  available: boolean;
  reason?: string;
  items: Record<string, RefreshableItem>;
  summary?: { total: number; withStatus: number; failing: number };
}

export interface GroupMember {
  id: string;
  name: string;
  upn: string;
  type: string;
}

export interface FabricItem {
  id: string;
  name: string;
  kind: string;
  wsId: string;
  wsName: string;
  modifiedDateTime: string;
  users: PermGrant[];
}

export interface PermissionScan {
  scanned: true;
  scannedAt: string | null;
  workspaces: PermWorkspace[];
  reports: PermReport[];
  datasets: PermDataset[];
  fabricItems?: FabricItem[];
  /** Entra group id -> the people inside it (transitive). */
  groupMembers?: Record<string, GroupMember[]>;
  groupErrors?: Record<string, string>;
  groupPrincipals?: Record<string, string>;
  stats: {
    workspacesScanned: number;
    reports: number;
    datasets: number;
    workspaceGrants: number;
    reportGrants: number;
    datasetGrants: number;
    groupsSeen: string[];
    fabricItems?: number;
    batches: number;
    errors: string[];
    orgLinks?: number;
    publishedToWeb?: number;
    /** Why the widely-shared lists could not be read, or "" when they were. */
    widelySharedUnavailable?: string;
  };
}

export type PermissionsResponse = PermissionScan | { scanned: false };

export interface ScanStatus {
  kind: string;
  label: string;
  running: boolean;
  startedAt: string | null;
  lastSuccessAt: string | null;
  lastStatus: string | null;
  lastError: string | null;
}

/** Everything one view needs, fetched together. */
export interface TenantData {
  summary: Summary;
  workspaces: Workspace[];
  reports: Report[];
  models: SemanticModel[];
  users: Principal[];
  activity: Activity;
  licenses: Licenses;
  runs: ScanRun[];
  refreshables: Refreshables;
}

export interface Capability {
  available: boolean;
  reason?: string;
}

export interface AudienceStatus {
  tenant: {
    id: string;
    displayName: string;
    isDemo: boolean;
    capabilities: Record<string, Capability>;
  };
  scans: (ScanStatus & { lastRunAt: string | null })[];
  anyRunning: boolean;
}

