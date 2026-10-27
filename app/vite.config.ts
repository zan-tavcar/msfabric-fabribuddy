import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `--mode preview` serves the app from public/local-chunks.json (the Contoso
// demo, written by core/tools/export_local.py) with no Fabric sign-in.
// ES2022: Rayfin's entity decorators need it, in dev as well as in the build.
export default defineConfig({
  plugins: [react()],
  esbuild: { target: "es2022" },
  optimizeDeps: { esbuildOptions: { target: "es2022" } },
  build: { target: "es2022", sourcemap: false },
});
