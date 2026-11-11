import { Boxes, Copy, FileCode2, NotebookText, Waypoints, Workflow, Zap } from "lucide-react";

const ICONS: Record<string, typeof Workflow> = {
  DataPipeline: Workflow,
  Notebook: NotebookText,
  Dataflow: Waypoints,
  SemanticModel: Boxes,
  SparkJobDefinition: Zap,
  CopyJob: Copy,
};

export function TypeIcon({ type, size = 15 }: { type: string; size?: number }) {
  const Icon = ICONS[type] ?? FileCode2;
  return (
    <span className={`mon-type mon-type-${type}`} title={type}>
      <Icon size={size} strokeWidth={1.9} aria-hidden="true" />
    </span>
  );
}
