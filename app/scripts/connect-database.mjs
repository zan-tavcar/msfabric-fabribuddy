// Point the app at your FabriBuddy_DB (the SQL database the scan notebook writes).
//
//   npm run connect -- <workspace-id> <sql-database-id>
//
// Deploy_FabriBuddy prints this command with both ids filled in. They are also
// in the database's address: …/groups/<workspace-id>/sqldatabases/<sql-database-id>
import { copyFileSync, readFileSync } from "node:fs";
import { execSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const app = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const [workspace, item] = process.argv.slice(2);
const guid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
if (!guid.test(workspace ?? "") || !guid.test(item ?? "")) {
  console.error("Usage: npm run connect -- <workspace-id> <sql-database-id>");
  process.exit(1);
}

const run = (command) => execSync(command, { cwd: app, stdio: "inherit" });
if (/name:\s*fabribuddy\s*$/m.test(readFileSync(join(app, "rayfin", "rayfin.yml"), "utf8"))) {
  run("npx rayfin connector remove fabribuddy --yes");
}
run(
  `npx rayfin connector add --type fabric-sqldatabase --name fabribuddy ` +
    `--workspace-id ${workspace} --item-id ${item} --operations read --yes`,
);
// `connector add` writes a placeholder schema; the app's entity is fixed (one
// table, read-only), so put ours back.
for (const file of ["UiChunk.ts", "schema.ts"]) {
  copyFileSync(join(app, "scripts", "connector", file), join(app, "rayfin", "connectors", "fabribuddy", file));
}
console.log("\nConnected. Next: npx rayfin up --workspace-id " + workspace);
