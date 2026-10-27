import { ExternalLink } from "lucide-react";
import type { FabricLink } from "../api/types";

/**
 * "Open in Fabric", in Fabric's own green: the item in the Fabric portal, in
 * the customer's tenant. On the demo there is nothing real to open, so the
 * button stays and says why.
 */
export function OpenInFabric({ link }: { link: FabricLink }) {
  if (link.fabricUrl) {
    return (
      <a
        className="btn btn-fabric"
        href={link.fabricUrl}
        target="_blank"
        rel="noreferrer"
        title="Open this item in Microsoft Fabric (a new tab)"
      >
        <ExternalLink size={14} aria-hidden="true" /> Open in Fabric
      </a>
    );
  }
  if (link.fabricDemo) {
    return (
      <button
        type="button"
        className="btn btn-fabric"
        disabled
        title="In your own tenant this opens the item in Microsoft Fabric. The demo's items are invented."
      >
        <ExternalLink size={14} aria-hidden="true" /> Open in Fabric
      </button>
    );
  }
  return null;
}
