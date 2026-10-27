import type { LineageTree, TreeNode } from "../api/types";
import { CODE_ITEM_KINDS, STORAGE_ITEM_KINDS, isLeafNode } from "./tree";

/** Workspace → item → table scope, as ids the backend accepts. Empty = all; several = any of them. */
export interface Filters {
  workspace: string[];
  item: string[];
  table: string[];
  /** Free-text search over table name, schema.table and path. */
  q: string;
}

export const NO_FILTERS: Filters = { workspace: [], item: [], table: [], q: "" };

export interface Option {
  id: string;
  name: string;
  detail?: string;
}

/**
 * Items the Item filter offers: where tables live, and the code that writes
 * them (a notebook narrows to the tables it writes and reads).
 */
const FILTER_ITEMS = new Set([...STORAGE_ITEM_KINDS, ...CODE_ITEM_KINDS]);

/** A workspace's items, looking through the item-type folders. */
export function itemsOf(workspace: TreeNode): TreeNode[] {
  return (workspace.children ?? []).flatMap((c) =>
    c.kind === "itemtype" ? (c.children ?? []) : [c],
  );
}

function leaves(node: TreeNode): TreeNode[] {
  if (isLeafNode(node)) return [node];
  return (node.children ?? []).flatMap(leaves);
}

/**
 * The options each dropdown offers, given what is already chosen.
 *
 * Derived from the sidebar tree the app already holds, so filtering costs no
 * extra request and can never offer something the tree does not show. Items
 * are the storage items (lakehouse, warehouse, mirrored/SQL database); tables
 * are the leaves under the chosen item, or under the chosen workspace when no
 * item is picked — which is how a semantic model or report can be picked too.
 */
export function filterOptions(tree: LineageTree, filters: Filters) {
  const workspaces: Option[] = tree.workspaces.map((w) => ({ id: w.id, name: w.name }));
  const chosenWorkspaces = tree.workspaces.filter((w) => filters.workspace.includes(w.id));
  const pool = chosenWorkspaces.length ? chosenWorkspaces : tree.workspaces;
  const single = chosenWorkspaces.length === 1;

  const items: Option[] = pool.flatMap((w) =>
    itemsOf(w)
      .filter((i) => FILTER_ITEMS.has(i.kind))
      .map((i) => ({ id: i.id, name: i.name, detail: single ? i.kind : `${w.name} · ${i.kind}` })),
  );

  const chosenItems = pool.flatMap(itemsOf).filter((i) => filters.item.includes(i.id));
  const tableSource = chosenItems.length ? chosenItems : chosenWorkspaces;
  const tables: Option[] = tableSource
    .flatMap(leaves)
    .map((n) => ({ id: n.id, name: n.name }))
    .sort((a, b) => a.name.localeCompare(b.name));

  return { workspaces, items, tables };
}

/** Query-string form, as the graph, overview and catalog endpoints accept it. */
export function filterQuery(filters?: Partial<Filters>): URLSearchParams {
  const query = new URLSearchParams();
  // Ids never contain commas, so several values travel as one parameter.
  if (filters?.workspace?.length) query.set("workspace", filters.workspace.join(","));
  if (filters?.item?.length) query.set("item", filters.item.join(","));
  if (filters?.table?.length) query.set("table", filters.table.join(","));
  if (filters?.q?.trim()) query.set("q", filters.q.trim());
  return query;
}

/** Search text. */
export function setQuery(current: Filters, q: string): Filters {
  return { ...current, q };
}

/**
 * Choose values at one level. The levels below keep only what still belongs
 * to the new choice: a table from a workspace no longer chosen is meaningless.
 */
export function setFilter(
  tree: LineageTree,
  current: Filters,
  level: "workspace" | "item" | "table",
  values: string[],
): Filters {
  const next: Filters = { ...current, [level]: values };
  if (level === "table") return next;
  const options = filterOptions(tree, next);
  const items = new Set(options.items.map((o) => o.id));
  if (level === "workspace") next.item = next.item.filter((id) => items.has(id));
  const tables = new Set(filterOptions(tree, next).tables.map((o) => o.id));
  next.table = next.table.filter((id) => tables.has(id));
  return next;
}

export function isFiltered(filters: Filters): boolean {
  return Boolean(
    filters.workspace.length || filters.item.length || filters.table.length || filters.q.trim(),
  );
}
