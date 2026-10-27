// The Fabric App's one connection: Fabric sign-in, and the `fabribuddy`
// connector over the SQL database the scan notebook writes.
import { ConnectorsRayfinClient } from "@microsoft/rayfin-client";
import type { FabricAuthOptions } from "@microsoft/rayfin-auth-provider-fabric";

import {
  connectorConfig,
  type FabriBuddySchema,
} from "../../rayfin/connectors/fabribuddy/schema";

type AppConnectorsSchema = { fabribuddy: FabriBuddySchema };
export type FabriBuddyClient = ConnectorsRayfinClient<
  Record<string, never>,
  Record<string, never>,
  AppConnectorsSchema
>;

let client: FabriBuddyClient | undefined;

/** Written into the build by `rayfin up` (rayfin env --framework vite). */
function env(name: keyof ImportMetaEnv): string {
  const value = import.meta.env[name];
  if (!value) {
    throw new Error(`${name} is not set. Deploy with \`npm run deploy\` (rayfin up).`);
  }
  return value;
}

export function getClient(): FabriBuddyClient {
  if (!client) {
    client = new ConnectorsRayfinClient<
      Record<string, never>,
      Record<string, never>,
      AppConnectorsSchema
    >({
      baseUrl: env("VITE_RAYFIN_API_URL"),
      publishableKey: env("VITE_RAYFIN_PUBLISHABLE_KEY"),
      authStorage: true,
      useProxy: false,
      connectors: { fabribuddy: connectorConfig },
    });
  }
  return client;
}

export function fabricAuthOptions(): FabricAuthOptions {
  return {
    workspaceId: env("VITE_FABRIC_WORKSPACE_ID"),
    projectId: env("VITE_FABRIC_ITEM_ID"),
    fabricPortalUrl: import.meta.env.VITE_FABRIC_PORTAL_URL || "https://app.fabric.microsoft.com",
    returnOrigin: window.location.origin,
  };
}
