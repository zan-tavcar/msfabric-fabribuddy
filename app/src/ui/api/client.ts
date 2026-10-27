import { type Filters, filterQuery } from "../lib/filters";
import { storedSource } from "../lib/attribution";
import type {
  AccessPayload,
  AdminTenant,
  AdminWorkspaces,
  AuthConfig,
  BillingPeriod,
  Catalog,
  CodePayload,
  CollectorError,
  Column,
  CreatedInvitation,
  Product,
  GraphPayload,
  LineageStatus,
  LineageTree,
  Me,
  Member,
  MembersPayload,
  Role,
  NodePage,
  ItemPagePayload,
  Overview,
  PlanCatalogue,
  PlanCode,
  PlatformOverview,
  InfraSnapshot,
  TrafficReport,
  DemoReport,
  CostReport,
  PlatformScans,
  PlatformUser,
  ScanPass,
  ScanProgress,
  ScheduleIn,
  ScopeMode,
  SearchPayload,
  Subscription,
  Tenant,
  TracePayload,
} from "./types";

const BASE = "/api";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/**
 * Where reads come from when there is no backend: the in-tenant edition (a
 * Fabric App) answers every GET from what its scan notebook stored. Unset in
 * the SaaS, where every request goes to /api.
 */
export type Transport = (path: string) => Promise<unknown>;
let transport: Transport | null = null;

export function setTransport(next: Transport | null): void {
  transport = next;
}

export async function req<T>(path: string, init?: RequestInit): Promise<T> {
  if (transport) {
    if (init?.method && init.method !== "GET") {
      throw new ApiError("This edition is read-only.", 403);
    }
    return (await transport(path)) as T;
  }
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(
      "Cannot reach the FabriBuddy backend. Is it running on port 8000?",
      0,
    );
  }

  if (res.status === 204) return undefined as T;
  if (res.status === 401 && !path.startsWith("/auth/")) {
    // The session expired or was revoked: tell the app to show sign-in.
    window.dispatchEvent(new Event("fl:signed-out"));
  }

  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const structured = body && typeof body.detail === "object" ? body.detail : null;
    if (structured?.locked) {
      // The trial or plan ended while this page was open: show the lock.
      window.dispatchEvent(new Event("fl:locked"));
    }
    const detail =
      (body && typeof body.detail === "string" && body.detail) ||
      (structured && typeof structured.message === "string" && structured.message) ||
      `Request failed with ${res.status}`;
    throw new ApiError(detail, res.status);
  }
  return body as T;
}

/**
 * Node and column ids carry `:` and `.` but never `/`, `#` or `?`, so they go
 * into a path segment. They are still encoded: a table name can contain a space.
 */
const seg = (id: string) => encodeURIComponent(id);

export const api = {
  tree: () => req<LineageTree>("/lineage/tree"),
  status: () => req<LineageStatus>("/lineage/status"),
  overview: (filters?: Filters) => req<Overview>(`/lineage/overview?${filterQuery(filters)}`),
  catalog: (filters?: Filters) => req<Catalog>(`/lineage/catalog?${filterQuery(filters)}`),
  errors: () => req<CollectorError[]>("/lineage/errors"),

  graph: (
    params: { focus?: string; up?: number; down?: number; select?: string } & Partial<Filters>,
  ) => {
    const query = filterQuery(params);
    if (params.focus) query.set("focus", params.focus);
    if (params.up !== undefined) query.set("up", String(params.up));
    if (params.down !== undefined) query.set("down", String(params.down));
    if (params.select) query.set("select", params.select);
    return req<GraphPayload>(`/lineage/graph?${query}`);
  },

  node: (id: string) => req<NodePage>(`/lineage/nodes/${seg(id)}`),
  item: (id: string) => req<ItemPagePayload>(`/lineage/items/${seg(id)}`),
  columns: (id: string) =>
    req<{ columns: Column[] }>(`/lineage/nodes/${seg(id)}/columns`),
  /** Write (or, empty, clear) a table's description. Contributor or admin. */
  setNodeDescription: (id: string, description: string) =>
    req<NodePage>(`/lineage/nodes/${seg(id)}/description`, {
      method: "PUT",
      body: JSON.stringify({ description }),
    }),
  setColumnDescription: (id: string, column: string, description: string) =>
    req<{ columns: Column[] }>(
      `/lineage/nodes/${seg(id)}/columns/${encodeURIComponent(column)}/description`,
      { method: "PUT", body: JSON.stringify({ description }) },
    ),
  code: (id: string) => req<CodePayload>(`/lineage/nodes/${seg(id)}/code`),
  access: (id: string) => req<AccessPayload>(`/lineage/nodes/${seg(id)}/access`),
  trace: (columnId: string) =>
    req<TracePayload>(`/lineage/columns/${seg(columnId)}/trace`),

  search: (q: string) =>
    req<SearchPayload>(`/lineage/search?q=${encodeURIComponent(q)}`),

  listTenants: () => req<Tenant[]>("/tenants"),
  /** A scan pass of both products; incremental unless `full`. Always queued. */
  scan: (full = false) => {
    const q = new URLSearchParams();
    if (full) q.set("full", "true");
    const qs = q.toString();
    return req<{ ok: boolean; queued?: boolean; mode?: string; products?: Product[] }>(
      `/admin/scan${qs ? `?${qs}` : ""}`,
      { method: "POST" },
    );
  },

  catalogCsvUrl: (filters?: Filters) => `${BASE}/lineage/catalog.csv?${filterQuery(filters)}`,

  // --- accounts ---------------------------------------------------------
  authConfig: () => req<AuthConfig>("/auth/config"),
  me: () => req<Me>("/auth/me"),
  register: (email: string, password: string, displayName: string, organisation = "") =>
    req<{ ok: boolean; user?: Me; verificationSent?: boolean }>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password, displayName, organisation, source: storedSource() }),
    }),
  login: (email: string, password: string) =>
    req<{ ok: boolean; user: Me }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  logout: () => req<{ ok: boolean }>("/auth/logout", { method: "POST" }),
  /** Sign in or sign up with a Microsoft work account: where to send the browser. */
  microsoftStart: () =>
    req<{ url: string }>("/auth/microsoft/start", {
      method: "POST",
      body: JSON.stringify({ source: storedSource() }),
    }),
  /** Add Microsoft sign-in to the signed-in account. */
  microsoftLink: () => req<{ url: string }>("/auth/microsoft/link", { method: "POST" }),
  /** Who is finishing a Microsoft sign-up, for naming the organisation. */
  microsoftSignup: (token: string) =>
    req<{ email: string; name: string; organisation: string }>(
      `/auth/microsoft/signup?token=${encodeURIComponent(token)}`,
    ),
  microsoftRegister: (token: string, organisation: string) =>
    req<{ ok: boolean; user: Me }>("/auth/microsoft/register", {
      method: "POST",
      body: JSON.stringify({ token, organisation }),
    }),
  verify: (token: string) =>
    req<{ ok: boolean; user: Me }>("/auth/verify", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
  resendVerification: (email: string) =>
    req<{ ok: boolean }>("/auth/resend-verification", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  forgot: (email: string) =>
    req<{ ok: boolean }>("/auth/forgot", { method: "POST", body: JSON.stringify({ email }) }),
  reset: (token: string, newPassword: string) =>
    req<{ ok: boolean }>("/auth/reset", {
      method: "POST",
      body: JSON.stringify({ token, newPassword }),
    }),
  changePassword: (currentPassword: string, newPassword: string) =>
    req<{ ok: boolean }>("/auth/password", {
      method: "POST",
      body: JSON.stringify({ currentPassword, newPassword }),
    }),
  updateProfile: (displayName: string) =>
    req<Me>("/auth/me", { method: "PATCH", body: JSON.stringify({ displayName }) }),

  // --- admin ------------------------------------------------------------
  adminTenant: () => req<AdminTenant>("/admin/tenant"),
  saveTenant: (body: { tenantId: string; clientId: string; clientSecret: string; displayName: string }) =>
    req<AdminTenant>("/admin/tenant", { method: "PUT", body: JSON.stringify(body) }),
  validateTenant: () => req<AdminTenant>("/admin/tenant/validate", { method: "POST" }),
  disconnectTenant: () => req<{ ok: boolean }>("/admin/tenant", { method: "DELETE" }),
  adminWorkspaces: () => req<AdminWorkspaces>("/admin/workspaces"),
  saveScope: (mode: ScopeMode, workspaceIds: string[], includePersonal?: boolean) =>
    req<AdminTenant>("/admin/scope", {
      method: "PUT",
      body: JSON.stringify({ mode, workspaceIds, includePersonal }),
    }),
  saveSchedule: (schedule: ScheduleIn) =>
    req<AdminTenant>("/admin/schedule", { method: "PUT", body: JSON.stringify(schedule) }),
  timezones: () => req<string[]>("/admin/timezones"),

  // --- members ----------------------------------------------------------
  members: () => req<MembersPayload>("/admin/members"),
  invite: (email: string, role: Role) =>
    req<CreatedInvitation>("/admin/invitations", {
      method: "POST",
      body: JSON.stringify({ email, role }),
    }),
  revokeInvitation: (id: string) =>
    req<{ ok: boolean }>(`/admin/invitations/${encodeURIComponent(id)}`, { method: "DELETE" }),
  setRole: (userId: string, role: Role) =>
    req<Member>(`/admin/members/${encodeURIComponent(userId)}`, {
      method: "PATCH",
      body: JSON.stringify({ role }),
    }),
  removeMember: (userId: string) =>
    req<{ ok: boolean }>(`/admin/members/${encodeURIComponent(userId)}`, { method: "DELETE" }),
  invitation: (token: string) =>
    req<{ email: string; role: Role }>(`/auth/invitation?token=${encodeURIComponent(token)}`),
  acceptInvitation: (token: string, password: string, displayName: string) =>
    req<{ ok: boolean; user: Me }>("/auth/accept", {
      method: "POST",
      body: JSON.stringify({ token, password, displayName }),
    }),

  // --- plan & subscription ----------------------------------------------
  subscription: () => req<Subscription>("/auth/subscription"),
  plans: () => req<PlanCatalogue>("/auth/plans"),
  /** Choose a plan: a Subscription (manual mode) or a Checkout / portal URL to go to. */
  choosePlan: (planCode: PlanCode, billingPeriod: BillingPeriod = "monthly") =>
    req<
      | Subscription
      | { checkoutUrl: string }
      | { pending: true }
      | { requested: true; plan: string; contact: string }
      | {
          paddleCheckout: {
            transactionId: string;
            clientToken: string;
            environment: string;
            email?: string;
          };
        }
    >("/auth/subscription", {
      method: "POST",
      body: JSON.stringify({ planCode, billingPeriod }),
    }),

  billingPortal: () => req<{ url: string }>("/auth/billing-portal", { method: "POST" }),

  // --- scan progress & history (any member) ------------------------------
  scanProgress: () => req<ScanProgress>("/scan/progress"),
  scanHistory: (limit = 20) => req<ScanPass[]>(`/scan/history?limit=${limit}`),

  // --- control plane (operator only; 404 for everyone else) --------------
  platformCustomers: () => req<PlatformOverview>("/platform/customers"),
  platformInfra: () => req<InfraSnapshot>("/platform/infra"),
  platformTraffic: (days: number) => req<TrafficReport>(`/platform/traffic?days=${days}`),
  platformDemo: (days: number) => req<DemoReport>(`/platform/demo?days=${days}`),
  platformCosts: () => req<CostReport | null>("/platform/costs"),
  /** A demo visitor opened a page: counted for the operator, never shown. */
  demoView: (page: string) =>
    req<void>("/demo/view", { method: "POST", body: JSON.stringify({ page }) }),
  platformScans: (params: { customerId?: string; status?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.customerId) q.set("customer_id", params.customerId);
    if (params.status) q.set("status", params.status);
    q.set("limit", String(params.limit ?? 200));
    return req<PlatformScans>(`/platform/scans?${q}`);
  },
  platformUsers: (customerId: string) =>
    req<PlatformUser[]>(`/platform/customers/${encodeURIComponent(customerId)}/users`),
  platformSetPlan: (
    customerId: string,
    body: {
      planCode: string;
      billingPeriod?: BillingPeriod;
      subscriptionStatus?: string;
      /** How long the plan (or trial) runs, counted from today. */
      extendDays?: number;
      notes?: string;
    },
  ) =>
    req<Subscription>(`/platform/customers/${encodeURIComponent(customerId)}/plan`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
};
