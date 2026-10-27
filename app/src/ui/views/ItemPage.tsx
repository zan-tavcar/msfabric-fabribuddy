import { useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { ApiError, api } from "../api/client";
import type { ItemLink, ItemPagePayload } from "../api/types";
import { Card, Chip, Loading } from "../components/primitives";
import { CodeBlock } from "./NodePage";
import { OpenInFabric } from "../components/OpenInFabric";

/**
 * A notebook, pipeline, dataflow, Spark job or eventstream on its own: its
 * code, the tables it writes and reads, and what the parser could not resolve.
 * A lakehouse or warehouse lists the tables it holds and who writes them, and
 * the code it has of its own (a warehouse's procedures, a mirroring definition).
 * Reachable from the sidebar even when none of its writes resolved — which is
 * exactly when someone needs to read the code.
 */
export function ItemPage({
  itemId,
  onPickNode,
  onPickItem,
}: {
  itemId: string;
  onPickNode: (nodeId: string) => void;
  onPickItem?: (itemId: string) => void;
}) {
  const [page, setPage] = useState<ItemPagePayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [part, setPart] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setPage(null);
    setError(null);
    setPart(0);
    api
      .item(itemId)
      .then((p) => !cancelled && setPage(p))
      .catch((e) => !cancelled && setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, [itemId]);

  if (error) return <p className="banner banner-danger">{error}</p>;
  if (!page) return <Loading what="this item" />;
  const shown = page.artifacts[Math.min(part, page.artifacts.length - 1)];
  // A lakehouse or warehouse holds tables; it shows code only when it has some.
  const storage = page.storage;

  return (
    <div className="stack">
      <div className="node-head">
        <div className="node-path">{page.path}</div>
        <div className="node-title-row">
          <h1 className="node-title">{page.name}</h1>
          <Chip tone="blue">{page.typeLabel}</Chip>
          <span className="node-head-actions">
            <OpenInFabric link={page} />
          </span>
        </div>
        {page.description && <p className="page-lede">{page.description}</p>}
      </div>

      {page.type === "VariableLibrary" ? (
        <LibraryValues page={page} onPickItem={onPickItem} />
      ) : storage ? (
        <div className="infra-grid">
          <Links
            title={`Tables (${page.tables.length})`}
            rows={page.tables}
            empty="No tables collected yet."
            onPick={onPickNode}
          />
          <Card head={<strong>Written by</strong>}>
            <div className="card-body stack-sm">
              {page.writtenBy.length === 0 && (
                <span className="muted-note">No notebook, pipeline or dataflow writing it was found.</span>
              )}
              {page.writtenBy.map((w) =>
                w.own ? (
                  <div key={w.id} className="item-link">
                    <span className="ellipsis">{w.name}</span>
                  </div>
                ) : (
                  <button key={w.id} type="button" className="item-link" onClick={() => onPickItem?.(w.id)}>
                    <span className="cell-mono ellipsis">{w.name}</span>
                    <span className="muted-note">{w.type}</span>
                  </button>
                ),
              )}
            </div>
          </Card>
        </div>
      ) : (
        <div className="infra-grid">
          <Links
            title="Writes"
            rows={page.writes}
            empty="No table it writes could be resolved."
            onPick={onPickNode}
          />
          <Links
            title="Reads"
            rows={page.reads}
            empty="No source it reads could be resolved."
            onPick={onPickNode}
          />
        </div>
      )}

      {page.parsed.length > 0 && (
        <Card head={<strong>Found in the code</strong>}>
          <div className="card-body stack-sm">
            {page.parsed.map((w, i) => (
              <div key={`${w.where}-${i}`} className="parsed-write">
                <div>
                  <Chip tone={w.resolved ? "green" : "amber"}>
                    {w.resolved ? "resolved" : "not resolved"}
                  </Chip>{" "}
                  <span className="muted-note">
                    {[w.where, w.mode].filter(Boolean).join(" · ")}
                  </span>
                </div>
                <div className="cell-mono parsed-target">writes {w.target}</div>
                {w.reads.map((r) => (
                  <div key={r} className="cell-mono muted-note parsed-source">
                    ← reads {r}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </Card>
      )}

      {page.warnings.length > 0 && (
        <Card head={<strong>What could not be resolved</strong>}>
          <ul className="item-warnings">
            {page.warnings.map((w) => (
              <li key={w}>
                <AlertTriangle size={14} aria-hidden="true" />
                {w}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {(!storage || page.artifacts.length > 0) && (
      <Card head={<strong>{page.type === "VariableLibrary" ? "Definition" : "Code"}</strong>}>
        <div className="card-body stack-sm">
          {page.artifacts.length === 0 ? (
            <p className="muted-note">{page.codeUnavailable}</p>
          ) : (
            <>
              {page.artifacts.length > 1 && (
                <div className="segmented item-parts" role="tablist" aria-label="Definition parts">
                  {page.artifacts.map((a, i) => (
                    <button
                      key={a.partPath}
                      type="button"
                      role="tab"
                      aria-selected={i === part}
                      className={i === part ? "active" : ""}
                      onClick={() => setPart(i)}
                    >
                      {a.partPath}
                    </button>
                  ))}
                </div>
              )}
              <p className="muted-note">
                {shown.partPath} · {shown.language}
                {shown.fetchedAt ? ` · read ${new Date(shown.fetchedAt).toLocaleString()}` : ""}
              </p>
              <CodeBlock text={shown.text} highlight={null} />
            </>
          )}
        </div>
      </Card>
      )}
    </div>
  );
}

function Links({
  title,
  rows,
  empty,
  onPick,
}: {
  title: string;
  rows: ItemLink[];
  empty: string;
  onPick: (nodeId: string) => void;
}) {
  return (
    <Card head={<strong>{title}</strong>}>
      <div className="card-body stack-sm">
        {rows.length === 0 && <span className="muted-note">{empty}</span>}
        {rows.map((r) => (
          <button key={r.id} type="button" className="item-link" onClick={() => onPick(r.id)}>
            <span className="cell-mono ellipsis">{r.name}</span>
            <span className="muted-note">
              {[r.mode, r.where, r.confidence !== "high" ? `${r.confidence} confidence` : ""]
                .filter(Boolean)
                .join(" · ")}
            </span>
          </button>
        ))}
      </div>
    </Card>
  );
}

/** A Variable Library: each variable's value in effect, and who names the library. */
function LibraryValues({
  page,
  onPickItem,
}: {
  page: ItemPagePayload;
  onPickItem?: (itemId: string) => void;
}) {
  return (
    <>
      <Card
        head={
          <strong>
            Variables
            {page.activeValueSet ? ` · active value set: ${page.activeValueSet}` : " · defaults"}
          </strong>
        }
      >
        {page.variables.length === 0 ? (
          <p className="muted-note card-body">No variables collected yet.</p>
        ) : (
          <table className="item-variables">
            <thead>
              <tr>
                <th>Variable</th>
                <th>Type</th>
                <th>In effect</th>
                <th>Default</th>
              </tr>
            </thead>
            <tbody>
              {page.variables.map((v) => (
                <tr key={v.name}>
                  <td className="cell-mono">
                    {v.name}
                    {v.note && <div className="muted-note">{v.note}</div>}
                  </td>
                  <td className="muted-note">{v.type}</td>
                  <td className="cell-mono">
                    {v.active}
                    {v.overridden && <Chip tone="amber">{page.activeValueSet}</Chip>}
                  </td>
                  <td className="cell-mono muted-note">{v.default}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      <Card head={<strong>Used by</strong>}>
        <div className="card-body stack-sm">
          {page.usedBy.length === 0 && (
            <span className="muted-note">
              No notebook, pipeline or dataflow in this workspace names this library.
            </span>
          )}
          {page.usedBy.map((u) => (
            <button
              key={u.id}
              type="button"
              className="item-link"
              onClick={() => onPickItem?.(u.id)}
            >
              <span className="cell-mono">{u.name}</span>
              <span className="muted-note">{u.type}</span>
            </button>
          ))}
        </div>
      </Card>
    </>
  );
}
