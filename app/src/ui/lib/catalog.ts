import type { CatalogRow } from "../api/types";

/** The storage kinds — what "tables" means on the Docs page. */
export const STORAGE_KINDS = new Set(["lakehouse_table", "warehouse_table", "mirrored_table"]);

export const isStorage = (row: CatalogRow) => STORAGE_KINDS.has(row.kind);

export interface Bar {
  label: string;
  value: number;
}

/** Counts per key, largest first; ties by label so the order is stable. */
export function countBy(rows: CatalogRow[], key: (row: CatalogRow) => string): Bar[] {
  const counts = new Map<string, number>();
  rows.forEach((row) => counts.set(key(row), (counts.get(key(row)) ?? 0) + 1));
  return [...counts.entries()]
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => b.value - a.value || a.label.localeCompare(b.label));
}

/** Top `limit` bars, the rest folded into one "Other" bar rather than dropped. */
export function topWithOther(bars: Bar[], limit: number): Bar[] {
  if (bars.length <= limit) return bars;
  const rest = bars.slice(limit - 1).reduce((sum, b) => sum + b.value, 0);
  return [...bars.slice(0, limit - 1), { label: `Other (${bars.length - limit + 1})`, value: rest }];
}

export const FRESHNESS = ["Last 24 h", "1–7 days", "7–30 days", "Older than 30 days", "No write recorded"];

/** Buckets by last Delta write, in a fixed order (ordinal, not sorted by size). */
/** Its Delta log shows a write within the last `days` days. */
export function writtenWithin(row: CatalogRow, days: number, now: number = Date.now()): boolean {
  if (!row.lastWriteAt) return false;
  const at = new Date(row.lastWriteAt + (row.lastWriteAt.endsWith("Z") ? "" : "Z")).getTime();
  return now - at <= days * 864e5;
}

/** Which FRESHNESS bucket a row falls in, by its last Delta write. */
export function freshnessBucket(row: CatalogRow, now: number = Date.now()): number {
  if (!row.lastWriteAt) return 4;
  const hours = (now - new Date(row.lastWriteAt + (row.lastWriteAt.endsWith("Z") ? "" : "Z")).getTime()) / 3.6e6;
  return hours <= 24 ? 0 : hours <= 24 * 7 ? 1 : hours <= 24 * 30 ? 2 : 3;
}

export function freshness(rows: CatalogRow[], now: number = Date.now()): Bar[] {
  const counts = FRESHNESS.map((label) => ({ label, value: 0 }));
  rows.forEach((row) => {
    counts[freshnessBucket(row, now)].value += 1;
  });
  return counts;
}

/** The catalog search: table, schema.table, item, workspace or the item writing it. */
export function matchesSearch(row: CatalogRow, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [
    row.name,
    row.schema ? `${row.schema}.${row.name}` : "",
    row.item,
    row.workspace,
    ...row.producers.map((p) => p.item),
  ].some((text) => text.toLowerCase().includes(needle));
}

export interface ItemGroup {
  id: string;
  name: string;
  type: string;
  rows: CatalogRow[];
}

export interface TypeGroup {
  id: string;
  /** Plural folder label: Lakehouses, Semantic models, Reports. */
  name: string;
  type: string;
  items: ItemGroup[];
  count: number;
}

export interface WorkspaceGroup {
  id: string;
  name: string;
  types: TypeGroup[];
  count: number;
}

/** Same order and labels as the sidebar, so both read the same way. */
export const TYPE_ORDER: [string, string][] = [
  ["Lakehouse", "Lakehouses"],
  ["Warehouse", "Warehouses"],
  ["MirroredDatabase", "Mirrored databases"],
  ["SQLDatabase", "SQL databases"],
  ["SemanticModel", "Semantic models"],
  ["Report", "Reports"],
];

const typeRank = (type: string) => {
  const index = TYPE_ORDER.findIndex(([t]) => t === type);
  return index < 0 ? TYPE_ORDER.length : index;
};

const SINGULAR: Record<string, string> = {
  Lakehouses: "lakehouse",
  Warehouses: "warehouse",
  "Mirrored databases": "mirrored database",
  "SQL databases": "SQL database",
  "Semantic models": "semantic model",
  Reports: "report",
};

/** "1 report", "3 lakehouses" — a type folder's count in words. */
export function countLabel(count: number, folder: string): string {
  const singular = SINGULAR[folder];
  if (!singular) return `${count} ${folder}`;
  return `${count} ${count === 1 ? singular : folder.toLowerCase()}`;
}

/** A report or a semantic model is one row: no item level between it and its folder. */
export const isSingleRowType = (type: string) => type === "Report" || type === "SemanticModel";

/** workspace -> item type -> item -> rows, for the catalog. */
export function groupCatalog(rows: CatalogRow[]): WorkspaceGroup[] {
  const workspaces = new Map<string, WorkspaceGroup>();
  rows.forEach((row) => {
    let ws = workspaces.get(row.workspaceId);
    if (!ws) {
      ws = { id: row.workspaceId, name: row.workspace || "(no workspace)", types: [], count: 0 };
      workspaces.set(row.workspaceId, ws);
    }
    const type = row.itemType || "Other";
    let group = ws.types.find((t) => t.type === type);
    if (!group) {
      const label = TYPE_ORDER.find(([t]) => t === type)?.[1] ?? type;
      group = { id: `${ws.id}:type:${type}`, name: label, type, items: [], count: 0 };
      ws.types.push(group);
    }
    let item = group.items.find((i) => i.id === row.itemId);
    if (!item) {
      item = { id: row.itemId, name: row.item, type, rows: [] };
      group.items.push(item);
    }
    item.rows.push(row);
    group.count += 1;
    ws.count += 1;
  });
  const out = [...workspaces.values()].sort((a, b) => a.name.localeCompare(b.name));
  out.forEach((ws) => {
    ws.types.sort((a, b) => typeRank(a.type) - typeRank(b.type));
    ws.types.forEach((t) => t.items.sort((a, b) => a.name.localeCompare(b.name)));
  });
  return out;
}

export function share(part: number, whole: number): string {
  return whole ? `${Math.round((part / whole) * 100)}%` : "—";
}

/** The graph selector for one node and everything up- and downstream of it. */
export const lineageOf = (id: string) => `+id:${/\s/.test(id) ? `"${id}"` : id}+`;
