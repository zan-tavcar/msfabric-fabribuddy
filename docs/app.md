# The app

The app is a **Fabric App** (preview) named **fabribuddy**, in your FabriBuddy workspace. It shows
the Data Catalog and Report Audience from what **FabriBuddy_Scan** stored in `FabriBuddy_DB`.

It has no server of its own and makes no calls outside Fabric. Every page is one read of the
database, made as the person viewing it.

## Adding it

Fabric Apps are deployed with Microsoft's Rayfin CLI from a computer with Node.js 20 or later.
`Deploy_FabriBuddy` prints these commands with your IDs filled in:

```bash
git clone https://github.com/zan-tavcar/msfabric-fabribuddy.git
cd msfabric-fabribuddy/app
npm install
npx rayfin login
npm run connect -- <workspace-id> <sql-database-id>
npx rayfin up --workspace-id <workspace-id>
```

- **The IDs** are in the database's address:
  `https://app.fabric.microsoft.com/groups/<workspace-id>/sqldatabases/<sql-database-id>`.
- **`npm run connect`** writes your database into `rayfin/rayfin.yml`. Keep that file if you'll
  update the app later.
- **Fabric Apps must be enabled** by your Fabric admin: *Admin portal → Tenant settings → Fabric
  Apps (preview)*. They aren't available in
  [every region](https://learn.microsoft.com/fabric/admin/region-availability) yet.

## Sharing

People need two things:

1. **Run and interact** permission on the **fabribuddy** app.
2. **Read access** to `FabriBuddy_DB`, because the app reads it as them.

The simplest way to give both is to add them to the workspace as **Viewer**.

!!! warning
    Everyone who can open the app sees everything in it: the whole scanned estate, and who can see
    each report. Share it with the people who would see that in the Fabric admin portal anyway.

## What the app doesn't do

- **No Rescan button or settings page.** The pipeline and its schedule run the scan. Change what's
  scanned in its [settings](settings.md).
- **No filter bar** above the overview and graph. The catalog's search, the sidebar, *View in
  lineage* and every table and item page work as usual.
- **Descriptions aren't edited in the app yet.** Descriptions from Delta tables and semantic models
  are shown.
