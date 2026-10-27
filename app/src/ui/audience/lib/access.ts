/**
 * Effective "can view" resolution.
 *
 * The rule (CLAUDE.md): a person sees a report only with Read on BOTH the
 * report AND its semantic model. Either half can come from a workspace role or
 * from a direct grant on the item — so "has access to the report" alone is not
 * an answer, and this is exactly the question the Power BI portal answers
 * badly.
 *
 * The ways in, as Microsoft names them (Share and collaborate on Power BI
 * reports, learn.microsoft.com): a workspace role; direct access (Share ->
 * Grant access, or a "Specific people" link — the admin APIs record both as a
 * grant to the person and do not tell them apart); a "People in your
 * organization" link (anyone in the tenant who has the link — no list of
 * people exists to count); and Publish to web (public).
 *
 * Honest limits, surfaced in the UI rather than hidden:
 *  - Entra security groups ARE expanded to their members (transitively) via
 *    Microsoft Graph, so a grant to "All Employees" resolves to the individual
 *    people it actually reaches. A group whose membership could not be read is
 *    still shown as a group rather than silently counted as one person.
 *  - App audiences are not scanned, so access granted purely by publishing an
 *    app is not represented yet.
 *  - A report whose semantic model lives in another workspace resolves against
 *    that model's grants, which is why model access is tracked separately.
 */
import type { PermissionScan, PermGrant, PermPrincipal } from "../api/types";

export type GrantSource = "workspace_role" | "direct_access";

export interface Grant {
  source: GrantSource;
  /** Workspace name for a role, item name for a direct grant. */
  via: string;
  right: string;
  /**
   * Set when this grant reaches the person through an Entra security group
   * rather than being addressed to them by name. This is the difference
   * between "3 principals can see it" and "300 people can see it".
   */
  viaGroup?: string;
}

export interface AccessPath {
  principal: PermPrincipal;
  reportGrant: Grant | null;
  modelGrant: Grant | null;
  canView: boolean;
  /** Which half is missing when canView is false. */
  blockedBy: "report" | "model" | "both" | null;
}

export interface ReportAccess {
  reportId: string;
  reportName: string;
  wsName: string;
  wsId: string;
  modelId: string;
  modelName: string;
  /** True when the report's model could not be found in the scan. */
  modelMissing: boolean;
  paths: AccessPath[];
  viewers: number;
  groups: number;
  /** Who can view, by how their access to the report was given. */
  byType: { workspaceRole: number; directAccess: number };
  /** A "People in your organization" link: not countable as people. */
  orgLink: { right: string; sharer: string } | null;
  /** Published to web: anyone on the internet. */
  publishedToWeb: { sharer: string } | null;
}

export interface PrincipalAccess {
  principal: PermPrincipal;
  /** Reports this principal can actually view. */
  reports: {
    reportId: string;
    reportName: string;
    wsName: string;
    modelName: string;
    reportGrant: Grant | null;
    modelGrant: Grant | null;
  }[];
  /** Reports they hold report-access to but cannot view (model missing). */
  blocked: { reportId: string; reportName: string; wsName: string; reason: string }[];
  workspaces: { name: string; right: string; viaGroup?: string }[];
}

export interface AccessIndex {
  byReport: Map<string, ReportAccess>;
  byPrincipal: Map<string, PrincipalAccess>;
  reportList: ReportAccess[];
  principalList: PrincipalAccess[];
  /** Raw scan rows, for panels that need item metadata rather than grants. */
  datasets: import("../api/types").PermDataset[];
  reports: import("../api/types").PermReport[];
  fabricItems: import("../api/types").FabricItem[];
  groupMembers: Record<string, import("../api/types").GroupMember[]>;
  stats: {
    reports: number;
    principals: number;
    groups: number;
    directShares: number;
    reportsWithDirectShares: number;
    /** Reports with a "People in your organization" link. */
    orgLinks: number;
    /** Reports published to web. */
    publishedToWeb: number;
    reportsNobodyCanView: number;
    groupsExpanded: number;
    peopleViaGroups: number;
    widestReport: ReportAccess | null;
  };
}

const norm = (s: string) => (s ?? "").trim().toLowerCase();

/** Any right containing "read", plus Owner, grants read. */
export function grantsRead(right: string): boolean {
  const r = norm(right);
  if (!r || r === "none") return false;
  return r.includes("read") || r === "owner";
}

/** Every workspace role implies read access to the workspace's content. */
export function roleGrantsRead(role: string): boolean {
  const r = norm(role);
  return ["admin", "member", "contributor", "viewer"].includes(r);
}

export function principalKey(p: PermPrincipal): string {
  return norm(p.upn) || norm(p.graphId) || norm(p.name);
}

export function buildAccessIndex(perm: PermissionScan): AccessIndex {
  const wsById = new Map(perm.workspaces.map((w) => [w.id, w]));
  const datasetById = new Map(perm.datasets.map((d) => [d.id, d]));
  const groupMembers = perm.groupMembers ?? {};

  /**
   * One grant to a group becomes one entry per person inside it, each tagged
   * with the group it came through. The group itself is kept too, so the
   * source of the access stays visible.
   */
  const expand = (grants: PermGrant[]): { p: PermPrincipal; right: string; via?: string }[] => {
    const out: { p: PermPrincipal; right: string; via?: string }[] = [];
    for (const g of grants) {
      out.push({ p: g, right: g.right });
      if (g.type === "Group" && g.graphId) {
        for (const m of groupMembers[g.graphId] ?? []) {
          out.push({
            p: { name: m.name, upn: m.upn, graphId: m.id, type: "User", right: g.right },
            right: g.right,
            via: g.name,
          });
        }
      }
    }
    return out;
  };

  const byReport = new Map<string, ReportAccess>();
  const byPrincipal = new Map<string, PrincipalAccess>();

  const upsertPrincipal = (p: PermPrincipal): PrincipalAccess => {
    const key = principalKey(p);
    let entry = byPrincipal.get(key);
    if (!entry) {
      entry = { principal: p, reports: [], blocked: [], workspaces: [] };
      byPrincipal.set(key, entry);
    }
    return entry;
  };

  // Workspace roles are a principal fact independent of any report.
  for (const w of perm.workspaces) {
    for (const { p, right, via } of expand(w.users)) {
      const entry = upsertPrincipal(p);
      if (!entry.workspaces.some((x) => x.name === w.name)) {
        entry.workspaces.push({ name: w.name, right, viaGroup: via });
      }
    }
  }

  let directShares = 0;
  let reportsWithDirectShares = 0;

  for (const rep of perm.reports) {
    const ws = wsById.get(rep.wsId);
    const model = rep.datasetId ? datasetById.get(rep.datasetId) : undefined;

    // Candidate principals: workspace members + report grantees + model grantees.
    const wsGrants = expand(ws?.users ?? []);
    // An organisation-wide entry (principalType None) is a link, not a person;
    // older scans stored it among the users.
    const people = rep.users.filter((u) => u.type !== "None" && (u.name || u.upn));
    const legacyLink = rep.users.find((u) => u.type === "None");
    const orgLink =
      rep.orgLink ?? (legacyLink ? { right: legacyLink.right || "Read", sharer: "" } : null);
    const repGrants = expand(people);
    const modelGrants = expand(model?.users ?? []);

    const candidates = new Map<string, PermPrincipal>();
    for (const g of [...wsGrants, ...repGrants, ...modelGrants]) {
      candidates.set(principalKey(g.p), g.p);
    }

    type E = { right: string; via?: string };
    const index = (list: { p: PermPrincipal; right: string; via?: string }[]) => {
      const m = new Map<string, E>();
      for (const g of list) {
        const k = principalKey(g.p);
        // A grant addressed to the person directly beats one via a group.
        if (!m.has(k) || (m.get(k)!.via && !g.via)) m.set(k, { right: g.right, via: g.via });
      }
      return m;
    };
    const wsRoleByKey = index(wsGrants);
    const repGrantByKey = index(repGrants);
    const modelGrantByKey = index(modelGrants);

    const hasDirect = people.length > 0;
    if (hasDirect) reportsWithDirectShares += 1;
    directShares += people.length;

    const paths: AccessPath[] = [];
    for (const [key, principal] of candidates) {
      // --- access to the report itself
      // A workspace role always wins: an Admin who published the report also
      // holds an automatic "Owner" grant on it, but their access is the role.
      let reportGrant: Grant | null = null;
      const role = wsRoleByKey.get(key);
      const direct = repGrantByKey.get(key);
      if (role && roleGrantsRead(role.right)) {
        reportGrant = {
          source: "workspace_role",
          via: ws?.name ?? rep.wsName,
          right: role.right,
          viaGroup: role.via,
        };
      } else if (direct && grantsRead(direct.right)) {
        reportGrant = {
          source: "direct_access", via: rep.name, right: direct.right, viaGroup: direct.via,
        };
      }

      // --- access to the semantic model behind it
      let modelGrant: Grant | null = null;
      if (model) {
        // The model's own workspace role (possibly another workspace) first.
        const modelWs = wsById.get(model.wsId);
        const modelRole = index(expand(modelWs?.users ?? [])).get(key);
        const dm = modelGrantByKey.get(key);
        if (modelRole && roleGrantsRead(modelRole.right)) {
          modelGrant = {
            source: "workspace_role",
            via: modelWs?.name ?? model.wsName,
            right: modelRole.right,
            viaGroup: modelRole.via,
          };
        } else if (dm && grantsRead(dm.right)) {
          modelGrant = {
            source: "direct_access", via: model.name, right: dm.right, viaGroup: dm.via,
          };
        }
      }

      const canView = Boolean(reportGrant && modelGrant);
      const blockedBy = canView
        ? null
        : !reportGrant && !modelGrant
          ? "both"
          : !reportGrant
            ? "report"
            : "model";

      if (reportGrant || modelGrant) {
        paths.push({ principal, reportGrant, modelGrant, canView, blockedBy });
      }
    }

    paths.sort(
      (a, b) =>
        Number(b.canView) - Number(a.canView) ||
        a.principal.name.localeCompare(b.principal.name),
    );

    const access: ReportAccess = {
      reportId: rep.id,
      reportName: rep.name,
      wsName: rep.wsName,
      wsId: rep.wsId,
      modelId: rep.datasetId,
      modelName: model?.name ?? "",
      modelMissing: Boolean(rep.datasetId) && !model,
      paths,
      viewers: paths.filter((p) => p.canView).length,
      groups: paths.filter((p) => p.principal.type === "Group").length,
      byType: {
        workspaceRole: paths.filter((p) => p.canView && p.reportGrant?.source === "workspace_role")
          .length,
        directAccess: paths.filter((p) => p.canView && p.reportGrant?.source === "direct_access")
          .length,
      },
      orgLink,
      publishedToWeb: rep.publishedToWeb ?? null,
    };
    byReport.set(rep.id, access);

    // Fan the result back out to each principal.
    for (const p of paths) {
      const entry = upsertPrincipal(p.principal);
      if (p.canView) {
        entry.reports.push({
          reportId: rep.id,
          reportName: rep.name,
          wsName: rep.wsName,
          modelName: model?.name ?? "",
          reportGrant: p.reportGrant,
          modelGrant: p.modelGrant,
        });
      } else if (p.reportGrant && !p.modelGrant) {
        entry.blocked.push({
          reportId: rep.id,
          reportName: rep.name,
          wsName: rep.wsName,
          reason: access.modelMissing
            ? "Its semantic model was not in the scan"
            : "No read access to the semantic model",
        });
      }
    }
  }

  const reportList = [...byReport.values()].sort((a, b) => b.viewers - a.viewers);
  const principalList = [...byPrincipal.values()].sort(
    (a, b) => b.reports.length - a.reports.length,
  );

  return {
    byReport,
    byPrincipal,
    reportList,
    principalList,
    datasets: perm.datasets,
    reports: perm.reports,
    fabricItems: perm.fabricItems ?? [],
    groupMembers,
    stats: {
      reports: reportList.length,
      principals: principalList.length,
      groups: principalList.filter((p) => p.principal.type === "Group").length,
      directShares,
      reportsWithDirectShares,
      orgLinks: reportList.filter((r) => r.orgLink).length,
      publishedToWeb: reportList.filter((r) => r.publishedToWeb).length,
      reportsNobodyCanView: reportList.filter((r) => r.viewers === 0).length,
      groupsExpanded: Object.keys(groupMembers).length,
      peopleViaGroups: principalList.filter((p) =>
        p.reports.some((r) => r.reportGrant?.viaGroup || r.modelGrant?.viaGroup),
      ).length,
      widestReport: reportList[0] ?? null,
    },
  };
}

export function sourceLabel(g: Grant | null): string {
  if (!g) return "no access";
  return g.source === "workspace_role" ? "Workspace role" : "Direct access";
}
