// Copy the SaaS frontend into src/ui — the same screens, unchanged.
//
//   node scripts/sync-ui.mjs [path/to/Fabric Lineage]
//
// The SaaS repository is the one source of the UI. This replaces src/ui
// entirely, so never edit it by hand: what is specific to the Fabric App
// (sign-in, reading the SQL database) lives in src/fabric and src/main.tsx.
import { cpSync, existsSync, rmSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const app = resolve(here, "..");
const saas = resolve(process.argv[2] ?? join(app, "..", "..", "Fabric Lineage"));
const source = join(saas, "frontend", "src");
const target = join(app, "src", "ui");

if (!existsSync(join(source, "App.tsx"))) {
  console.error(`Not the SaaS repository: ${saas}`);
  process.exit(1);
}

// Left out: the operator console, tests, and the SaaS entry point.
const skip = (path) => {
  const rel = relative(source, path);
  return (
    rel === "main.tsx" ||
    rel === "console" ||
    rel.startsWith(`console${sep}`) ||
    /\.test\.tsx?$/.test(rel)
  );
};

rmSync(target, { recursive: true, force: true });
cpSync(source, target, { recursive: true, filter: (path) => !skip(path) });

let commit = "unknown";
try {
  commit = execFileSync("git", ["-C", saas, "rev-parse", "--short", "HEAD"]).toString().trim();
} catch {
  // not a git checkout: the copy still works
}
writeFileSync(join(target, "SOURCE"), `Fabric Lineage frontend @ ${commit}\n`);
console.log(`Copied the SaaS frontend @ ${commit} into src/ui`);
