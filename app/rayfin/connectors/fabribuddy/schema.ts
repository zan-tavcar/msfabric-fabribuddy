// The `fabribuddy` connector: the Fabric SQL database the scan notebook writes.
// `scripts/connect-database.sh` runs `rayfin connector add` (which writes the
// rayfin.yml entry and metadata.json) and then restores this file and UiChunk.ts.
import type { GraphQLBackedConnector } from "@microsoft/rayfin-connector-fabric-graphql";
import type { ConnectorConfig } from "@microsoft/rayfin-connectors";

import { UiChunk } from "./UiChunk.js";

export { UiChunk } from "./UiChunk.js";

export const connectorConfig = {
  connector: "fabric-sqldatabase",
  operations: ["read"],
  entities: { UiChunk },
} as const satisfies ConnectorConfig;

export type FabriBuddySchema = GraphQLBackedConnector<
  { UiChunk: typeof UiChunk },
  typeof connectorConfig
>;
