import { useMemo, useState } from "react";
import {
  ArrowRightLeft,
  Boxes,
  FileCode2,
  Workflow,
  Zap,
  ChevronDown,
  ChevronRight,
  Columns3,
  Database,
  FileBarChart,
  Folder,
  Globe,
  Layers,
  Link2,
  Radio,
  Variable,
  Search,
  Table2,
} from "lucide-react";
import type { LineageTree, TreeNode } from "../api/types";
import {
  ITEM_PAGE_KINDS,
  branchOpen,
  filterTree,
  highlight,
  isLeafNode,
  roots,
  withColumns,
} from "../lib/tree";
import { LayerDot } from "../components/primitives";

const ICONS: Record<string, typeof Folder> = {
  workspace: Folder,
  Lakehouse: Database,
  Warehouse: Database,
  SemanticModel: Boxes,
  Report: FileBarChart,
  MirroredDatabase: Database,
  SQLDatabase: Database,
  Notebook: FileCode2,
  DataPipeline: Workflow,
  Dataflow: ArrowRightLeft,
  SparkJobDefinition: Zap,
  CopyJob: ArrowRightLeft,
  Eventstream: Radio,
  VariableLibrary: Variable,
  schema: Layers,
  group: Globe,
  connection: Link2,
};

const ICON_COLOR: Record<string, string> = {
  workspace: "var(--ink)",
  Lakehouse: "var(--green)",
  Warehouse: "var(--green)",
  SemanticModel: "var(--purple)",
  Report: "var(--blue)",
  MirroredDatabase: "var(--green)",
  SQLDatabase: "var(--green)",
  // Code that writes tables: the accent, the colour of "where it is made".
  Notebook: "var(--accent-text)",
  DataPipeline: "var(--accent-text)",
  Dataflow: "var(--accent-text)",
  SparkJobDefinition: "var(--accent-text)",
  CopyJob: "var(--accent-text)",
  Eventstream: "var(--accent-text)",
  VariableLibrary: "var(--muted)",
  schema: "var(--faint)",
  group: "var(--faint)",
  connection: "var(--faint)",
};

export function TreeSidebar({
  tree,
  selectedId,
  onSelect,
}: {
  tree: LineageTree;
  selectedId: string | null;
  onSelect: (node: TreeNode) => void;
}) {
  const [query, setQuery] = useState("");
  const [manual, setManual] = useState<Set<string>>(
    // Workspaces start open: a fully collapsed tree gives a first-time user
    // nothing to orient by.
    () => new Set(tree.workspaces.map((w) => w.id)),
  );

  const all = useMemo(() => withColumns(roots(tree)), [tree]);
  const { nodes, expand, matches } = useMemo(
    () => filterTree(all, query),
    [all, query],
  );

  // While searching, matched ancestors open automatically, but a click must
  // still win: before, a search made every branch unclickable, so a matched
  // lakehouse or semantic model could not be opened to reach its tables. Clicks
  // during a search are kept as overrides for that query only; the manual state
  // underneath is untouched, so clearing the box restores what you had open.
  const [overrides, setOverrides] = useState<{ query: string; open: Map<string, boolean> }>(
    () => ({ query: "", open: new Map() }),
  );
  const searchOpen = overrides.query === query ? overrides.open : new Map<string, boolean>();

  const isOpen = (id: string) => branchOpen(id, query, manual, expand, searchOpen);

  const toggle = (id: string) => {
    if (query) {
      const next = new Map(searchOpen);
      next.set(id, !isOpen(id));
      setOverrides({ query, open: next });
      return;
    }
    setManual((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <>
      <div className="tree-search">
        <label htmlFor="tree-q">Browse workspaces</label>
        <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
          <Search
            size={14}
            strokeWidth={2.2}
            aria-hidden="true"
            style={{
              position: "absolute",
              left: 10,
              color: "var(--faint)",
              pointerEvents: "none",
            }}
          />
          <input
            id="tree-q"
            className="input"
            style={{ paddingLeft: 30 }}
            value={query}
            placeholder="Search tables, items, columns"
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        {query && (
          <span className="muted-note" style={{ fontSize: 12 }}>
            {matches === 0
              ? "No matches"
              : `${matches} match${matches === 1 ? "" : "es"}`}
          </span>
        )}
      </div>

      <div className="tree-body" role="tree" aria-label="Lineage tree">
        {nodes.length === 0 && (
          <p className="tree-empty">
            Nothing here yet. Run a scan, or start the backend with FL_DEMO_MODE=1.
          </p>
        )}
        {nodes.map((node) => (
          <Branch
            key={node.id}
            node={node}
            depth={0}
            query={query}
            selectedId={selectedId}
            isOpen={isOpen}
            onToggle={toggle}
            onSelect={onSelect}
          />
        ))}
      </div>
    </>
  );
}

function Branch({
  node,
  depth,
  query,
  selectedId,
  isOpen,
  onToggle,
  onSelect,
}: {
  node: TreeNode;
  depth: number;
  query: string;
  selectedId: string | null;
  isOpen: (id: string) => boolean;
  onToggle: (id: string) => void;
  onSelect: (node: TreeNode) => void;
}) {
  const children = node.children ?? [];
  const hasChildren = children.length > 0;
  const open = isOpen(node.id);
  const leaf = isLeafNode(node);
  const column = node.kind === "column";
  const selected = selectedId === node.id;
  // A type folder wears its items' icon: "Reports" looks like a report.
  const iconKind = node.kind === "itemtype" ? (node.itemType ?? "") : node.kind;
  const Icon = leaf ? Table2 : (ICONS[iconKind] ?? Folder);
  const [before, match, after] = highlight(node.name, query);

  const rowClass = [
    "tree-row",
    node.kind === "workspace" ? "ws" : "",
    ["Lakehouse", "Warehouse", "MirroredDatabase", "SQLDatabase"].includes(node.kind)
      ? "lh"
      : "",
    selected ? "active" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div>
      <button
        type="button"
        className={rowClass}
        // Indent is inline because depth is data: a tenant can nest deeper than
        // any hand-written set of classes.
        style={{ paddingLeft: 8 + depth * 16 }}
        role="treeitem"
        aria-expanded={hasChildren ? open : undefined}
        aria-selected={selected}
        onClick={() => {
          // A column opens its table's Columns tab.
          if (column) return onSelect(node);
          if (leaf) {
            // A table opens its page and folds out its columns; a second click
            // on the open, selected table folds them away again.
            if (hasChildren && (!open || selected)) onToggle(node.id);
            return onSelect(node);
          }
          // A notebook, pipeline, dataflow… opens its own page and still folds out.
          if (ITEM_PAGE_KINDS.has(node.kind)) onSelect(node);
          if (hasChildren) onToggle(node.id);
        }}
      >
        <span className="tree-chev">
          {hasChildren ? (
            open ? (
              <ChevronDown size={13} strokeWidth={2.4} aria-hidden="true" />
            ) : (
              <ChevronRight size={13} strokeWidth={2.4} aria-hidden="true" />
            )
          ) : null}
        </span>

        <span
          className="tree-icon"
          style={{ color: selected ? "var(--accent-text)" : ICON_COLOR[iconKind] ?? "var(--faint)" }}
        >
          {column ? (
            <Columns3 size={13} strokeWidth={1.7} aria-hidden="true" />
          ) : leaf && node.layer ? (
            <LayerDot layer={node.layer} />
          ) : (
            <Icon size={15} strokeWidth={1.7} aria-hidden="true" />
          )}
        </span>

        <span
          className={column ? "tree-label mono tree-column" : leaf ? "tree-label mono" : "tree-label"}
          title={column ? node.name : undefined}
        >
          {before}
          {match && <mark className="tree-mark">{match}</mark>}
          {after}
        </span>

        {column ? (
          node.dataType && (
            // The base type only (decimal, not decimal(32,16)): the name matters
            // more in a narrow sidebar. The full type is on hover.
            <span className="tree-type" title={node.dataType}>
              {node.dataType.replace(/\(.*$/, "")}
            </span>
          )
        ) : (
          node.count > 0 && <span className="tree-count tnum">{node.count}</span>
        )}
      </button>

      {hasChildren && open && (
        <div role="group">
          {children.map((child) => (
            <Branch
              key={child.id}
              node={child}
              depth={depth + 1}
              query={query}
              selectedId={selectedId}
              isOpen={isOpen}
              onToggle={onToggle}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </div>
  );
}
