/**
 * Audience's reads: the latest stored scans, never a scan. The request
 * plumbing (errors, sign-out on 401) is the app's shared one.
 */
import { req } from "../../api/client";
import type {
  Activity,
  AudienceStatus,
  Licenses,
  PermissionsResponse,
  Principal,
  Refreshables,
  Report,
  ScanRun,
  SemanticModel,
  Summary,
  TenantData,
  Workspace,
} from "./types";

export const audienceApi = {
  status: () => req<AudienceStatus>("/audience/status"),

  /**
   * Everything that comes back quickly, in parallel. Activity and licences
   * load separately (`loadActivity`, `loadLicenses`) and fill in.
   */
  async loadCore(): Promise<TenantData> {
    const get = <T,>(p: string) => req<T>(`/audience${p}`);
    const [summary, workspaces, reports, models, users, runs, refreshables] =
      await Promise.all([
        get<Summary>("/summary"),
        get<Workspace[]>("/workspaces"),
        get<Report[]>("/reports"),
        get<SemanticModel[]>("/models"),
        get<Principal[]>("/users"),
        get<ScanRun[]>("/runs?limit=10"),
        get<Refreshables>("/refreshables").catch(
          () => ({ available: false, items: {} }) as Refreshables,
        ),
      ]);
    return {
      summary,
      workspaces,
      reports,
      models,
      users,
      runs,
      refreshables,
      activity: normaliseActivity(undefined as unknown as Activity),
      licenses: normaliseLicenses(undefined as unknown as Licenses),
    };
  },
  loadActivity: (days: number) =>
    req<Activity>(`/audience/activity?days=${days}`).then(normaliseActivity),
  loadLicenses: (days: number) =>
    req<Licenses>(`/audience/licenses?days=${days}`).then(normaliseLicenses),
  getPermissions: () => req<PermissionsResponse>("/audience/permissions"),
};

function normaliseActivity(a: Activity): Activity {
  if (a?.live) return a;
  return {
    live: false,
    error: a?.error ?? "Activity data is unavailable.",
    events: [],
    summary: { total: 0, views: 0, exports: 0, distinctUsers: 0, days: 0 },
    topReports: [],
  };
}

function normaliseLicenses(l: Licenses): Licenses {
  if (l?.live) return l;
  return {
    live: false,
    error: l?.error ?? "Licence data is unavailable.",
    rows: [],
    summary: { pro: 0, ppu: 0, total: 0, unused: 0, activityJoined: false },
  };
}
