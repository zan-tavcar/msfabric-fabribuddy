import { useState } from "react";
import { Loader2, Pencil } from "lucide-react";
import type { DescriptionMeta } from "../api/types";
import { stamp } from "../lib/format";

/**
 * A description that a contributor or admin can write, with where it came
 * from: "Edited by Ana · 2 Oct" for one written in FabriBuddy, or the source
 * of a collected one (the semantic model, a Delta table property).
 *
 * Saving empty text clears what was written here, and a collected description
 * (if any) shows again — the edit is a layer on top, never a replacement.
 */
export function DescriptionEditor({
  text,
  hasText,
  meta,
  canEdit,
  onSave,
  compact = false,
  label,
}: {
  text: string;
  hasText: boolean;
  meta: DescriptionMeta | null | undefined;
  canEdit: boolean;
  onSave: (text: string) => Promise<void>;
  /** A column row: smaller type, no lede styling. */
  compact?: boolean;
  /** What is described, for the editor's accessible name. */
  label: string;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const start = () => {
    setDraft(meta?.source === "user" || hasText ? text : "");
    setError(null);
    setEditing(true);
  };
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await onSave(draft.trim());
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (editing) {
    return (
      <div className={compact ? "desc-edit compact" : "desc-edit"}>
        <textarea
          className="input desc-input"
          aria-label={`Description of ${label}`}
          value={draft}
          rows={compact ? 2 : 3}
          maxLength={4000}
          autoFocus
          placeholder="What is this, where does it come from, what should people know?"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") setEditing(false);
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void save();
          }}
        />
        <div className="desc-actions">
          <button type="button" className="btn btn-primary btn-sm" onClick={save} disabled={busy}>
            {busy && <Loader2 className="spin" size={13} aria-hidden="true" />}
            Save
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => setEditing(false)}
            disabled={busy}
          >
            Cancel
          </button>
          <span className="muted-note">Empty clears it. Ctrl+Enter saves.</span>
        </div>
        {error && <p className="banner banner-danger">{error}</p>}
      </div>
    );
  }

  return (
    <div className={compact ? "desc compact" : "desc"}>
      <div className="desc-row">
        <span className={hasText ? "desc-text" : "desc-text desc-missing"}>
          {hasText ? text : compact ? "No description" : text}
        </span>
        {canEdit && (
          <button
            type="button"
            className="btn btn-icon desc-pencil"
            aria-label={hasText ? `Edit the description of ${label}` : `Describe ${label}`}
            title={hasText ? "Edit description" : "Add a description"}
            onClick={start}
          >
            <Pencil size={13} aria-hidden="true" />
          </button>
        )}
      </div>
      {hasText && meta && (
        <div className="desc-meta">
          {meta.source === "user"
            ? `Edited by ${meta.updatedBy || "someone"}${meta.updatedAt ? ` · ${stamp(meta.updatedAt)}` : ""}`
            : meta.sourceLabel || "Collected from the tenant"}
        </div>
      )}
    </div>
  );
}
