/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** The Fabric App's backend URL — written by `rayfin up`. */
  readonly VITE_RAYFIN_API_URL?: string;
  /** The app's publishable key (pk-…) — written by `rayfin up`. */
  readonly VITE_RAYFIN_PUBLISHABLE_KEY?: string;
  /** The workspace holding the app — written by `rayfin up`. */
  readonly VITE_FABRIC_WORKSPACE_ID?: string;
  /** The Fabric App item — written by `rayfin up`. */
  readonly VITE_FABRIC_ITEM_ID?: string;
  /** The Fabric portal (default https://app.fabric.microsoft.com). */
  readonly VITE_FABRIC_PORTAL_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
