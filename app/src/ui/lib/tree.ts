import type { LineageTree, TreeNode } from "../api/types";

/**
 * Tree search and expansion.
 *
 * Pure functions, kept out of the component so the matching rules are testable
 * — the sidebar is the primary way anyone navigates a tenant with thousands of
 * tables, and "my table is not in the list" is the failure that makes the whole
 * product feel broken.
 */

/** Every branch of the tree the UI renders, in display order. */
export function roots(tree: LineageTree): TreeNode[] {
  // External sources are pinned last: they are context for the graph rather
  // than something anyone browses to first.
  return [...tree.workspaces, tree.external];
}

/**
 * Unfold each table's columns into a level of the tree. The API sends them as
 * compact [name, type] pairs (an estate can hold tens of thousands); here they
 * become ordinary nodes, so browsing and search treat them like everything else.
 */
export function withColumns(nodes: TreeNode[]): TreeNode[] {
  return nodes.map((node) => {
    if (node.columns?.length) {
      return {
        ...node,
        count: node.columns.length,
        children: node.columns.map(([name, dataType]) => ({
          id: `${node.id}#column:${name}`,
          kind: "column",
          name,
          dataType,
          tableId: node.id,
          count: 0,
        })),
      };
    }
    return node.children?.length ? { ...node, children: withColumns(node.children) } : node;
  });
}

/** Matches anywhere in a subtree, used when a matching branch is kept whole. */
function countMatches(nodes: TreeNode[], needle: string): number {
  let n = 0;
  for (const node of nodes) {
    if (node.name.toLowerCase().includes(needle)) n += 1;
    n += countMatches(node.children ?? [], needle);
  }
  return n;
}

export interface FilterResult {
  nodes: TreeNode[];
  /** Ids to force open so every match is visible without manual expansion. */
  expand: Set<string>;
  matches: number;
}

/**
 * Filter the tree to branches containing a match, and say what to expand.
 *
 * A node is kept when it matches OR when one of its descendants does — a
 * filter that dropped the parent would leave matching leaves unreachable.
 * A matching *branch* keeps all of its children, so searching "lh_silver"
 * shows that lakehouse's tables rather than an empty folder.
 */
export function filterTree(nodes: TreeNode[], query: string): FilterResult {
  const needle = query.trim().toLowerCase();
  if (!needle) return { nodes, expand: new Set(), matches: 0 };

  const expand = new Set<string>();
  let matches = 0;

  function visit(node: TreeNode): TreeNode | null {
    const selfMatch = node.name.toLowerCase().includes(needle);
    if (selfMatch) matches += 1;

    const children = node.children ?? [];
    // A matching branch keeps its whole subtree, so the result is browsable
    // rather than a single stranded row. Its descendants are still counted:
    // saying "1 match" over a visibly large subtree reads as a bug.
    if (selfMatch && children.length > 0) {
      expand.add(node.id);
      matches += countMatches(children, needle);
      return node;
    }

    const kept = children
      .map(visit)
      .filter((c): c is TreeNode => c !== null);

    if (kept.length > 0) {
      expand.add(node.id);
      return { ...node, children: kept };
    }
    return selfMatch ? node : null;
  }

  const filtered = nodes
    .map(visit)
    .filter((n): n is TreeNode => n !== null);

  return { nodes: filtered, expand, matches };
}

/** Split a label around the query so the match can be marked. */
export function highlight(label: string, query: string): [string, string, string] {
  const needle = query.trim();
  if (!needle) return [label, "", ""];
  const at = label.toLowerCase().indexOf(needle.toLowerCase());
  if (at < 0) return [label, "", ""];
  return [
    label.slice(0, at),
    label.slice(at, at + needle.length),
    label.slice(at + needle.length),
  ];
}

/** Ids of every branch node, for "expand all". */
export function allBranchIds(nodes: TreeNode[]): Set<string> {
  const out = new Set<string>();
  const walk = (list: TreeNode[]) => {
    for (const node of list) {
      if (node.children && node.children.length > 0) {
        out.add(node.id);
        walk(node.children);
      }
    }
  };
  walk(nodes);
  return out;
}

/** Depth-first path of ids from a root down to `targetId`, inclusive. */
export function pathTo(nodes: TreeNode[], targetId: string): string[] | null {
  for (const node of nodes) {
    if (node.id === targetId) return [node.id];
    const below = pathTo(node.children ?? [], targetId);
    if (below) return [node.id, ...below];
  }
  return null;
}

/** True for nodes that represent an actual lineage node, not a folder. */
export function isLeafNode(node: TreeNode): boolean {
  return (
    node.kind === "lakehouse_table" ||
    node.kind === "warehouse_table" ||
    node.kind === "mirrored_table" ||
    node.kind === "model_table" ||
    node.kind === "report_visual" ||
    node.kind === "external"
  );
}

/**
 * Whether a branch is open.
 *
 * Outside a search, the user's manual state. During one, a click on that
 * branch for this query (`overrides`) wins over the automatic expansion of
 * matched ancestors — otherwise nothing could be opened or closed while
 * searching, and a matched lakehouse could never be drilled into.
 */
export function branchOpen(
  id: string,
  query: string,
  manual: Set<string>,
  expand: Set<string>,
  overrides: Map<string, boolean>,
): boolean {
  if (!query) return manual.has(id);
  return overrides.get(id) ?? expand.has(id);
}

/** Items that hold tables: their page lists them (and any code of their own). */
export const STORAGE_ITEM_KINDS = new Set(["Lakehouse", "Warehouse", "MirroredDatabase", "SQLDatabase"]);

/** Items that write tables: notebooks, pipelines, dataflows… */
export const CODE_ITEM_KINDS = new Set([
  "Notebook",
  "DataPipeline",
  "Dataflow",
  "SparkJobDefinition",
  "CopyJob",
  "Eventstream",
]);

/** Sidebar kinds that open an item page rather than a table's. */
export const ITEM_PAGE_KINDS = new Set([
  ...STORAGE_ITEM_KINDS,
  ...CODE_ITEM_KINDS,
  "VariableLibrary",
]);
