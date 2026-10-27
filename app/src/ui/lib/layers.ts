import type { Layer } from "../api/types";

/**
 * Lane order, left to right through the graph: the storage a node lives in.
 * Decided by the backend from the node's kind, never from its name.
 */
export const LAYER_ORDER: Layer[] = [
  "source",
  "mirrored",
  "lakehouse",
  "warehouse",
  "semantic",
  "report",
];

export const LAYER_LABEL: Record<Layer, string> = {
  source: "External source",
  mirrored: "Mirrored database",
  lakehouse: "Lakehouse table",
  warehouse: "Warehouse table",
  semantic: "Semantic model",
  report: "Report",
};

export function layerIndex(layer: Layer | undefined): number {
  return layer ? LAYER_ORDER.indexOf(layer) : -1;
}

/**
 * Chip tone for a permission label.
 *
 * Indirect access gets amber rather than blue: it is the one permission on the
 * page that needs a second look, and colour is how the table says so at a glance.
 */
export function permissionTone(permission: string): string {
  const value = permission.toLowerCase();
  if (value.includes("indirect")) return "amber";
  if (value === "admin" || value === "owner") return "purple";
  if (["member", "contributor", "build", "write", "edit"].includes(value)) return "blue";
  return "neutral";
}

/** Chip tone for a finding's short label. */
export function findingTone(tone: string): string {
  if (tone === "PII") return "danger";
  return "amber";
}
