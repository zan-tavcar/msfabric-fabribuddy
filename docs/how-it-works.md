# How it works

## The items

| Item | What it does |
|---|---|
| **Deploy_FabriBuddy** (notebook) | Creates and updates the others. Downloads each release from GitHub and stores it in `FabriBuddy_DB`. |
| **Load_FabriBuddy_E2E** (pipeline) | Holds the [settings](settings.md) and runs the scan. This is the one you schedule. |
| **FabriBuddy_Scan** (Python notebook) | Scans the tenant read-only, then prepares every page the app shows. |
| **FabriBuddy_DB** (SQL database) | `ui_chunk`: the app's pages. `fabribuddy_state`: the scanner's working data. `fabribuddy_run`: one row per run. `fabribuddy_package`: the installed release. |
| **Check_FabriBuddy_Version** (notebook) | Compares the installed release with the latest one. |
| **fabribuddy** (Fabric App) | The interface. Reads `ui_chunk` as the person viewing it. |

## Two identities

- **The service principal** calls Microsoft: the read-only admin APIs, Fabric REST, OneLake, the
  warehouse SQL endpoints and, optionally, Microsoft Graph.
- **Whoever runs the pipeline** (the schedule's owner) writes `FabriBuddy_DB` and reads the secret
  from Key Vault. So the principal needs no role on the FabriBuddy workspace.

## A scan

1. **Install** the release stored in `FabriBuddy_DB`. GitHub isn't needed.
2. **Sign in** as the service principal, with the secret read from Key Vault into memory.
3. **Restore** the scanner's working data from the last run, so only changes are read again.
4. **Data Catalog**, in order:
    - **Inventory:** workspaces, items, users, sensitivity labels.
    - **Items and tables:** lakehouse, warehouse and mirrored tables, and shortcuts.
    - **Definitions:** the code of notebooks, pipelines, dataflows, semantic models and reports.
    - **Warehouse SQL:** views, procedures and executed statements.
    - **Parse:** the code becomes table-level lineage.
    - **Delta log:** row counts, last writes, history.
    - **Jobs:** run history.
    - **Access:** who can read each table.
5. **Report Audience:** inventory, permissions (with Entra groups expanded), the audit log (only
   the days not already read), and licences.
6. **Save** the working data back to `FabriBuddy_DB`.
7. **Publish:** compute every page of the app and store it in `ui_chunk`, replacing the previous
   set in one transaction.

If a step fails, the steps after it still run. Anything that couldn't be read is listed on the
Data Catalog overview.

### How long

| | First scan | Later scans |
|---|---|---|
| About 50 workspaces, 300 items | 15–30 min | 5–10 min |
| 7 workspaces, 22 items | About 2 min | Under a minute |

Reading code (`getDefinition`) is the slow part, and Microsoft rate-limits it. Later scans re-read
only the items that changed.

## How the app reads its data

The interface asks for `/lineage/catalog`, `/lineage/nodes/<id>` and so on, exactly as it would ask
a server. The scan answers every one of those requests in advance and stores each answer in
`ui_chunk`, keyed by a hash of the request. The app turns each request into one keyed read of the
database, through its Fabric connector, as the person viewing it.

So there is no FabriBuddy server anywhere: the "server" ran inside your notebook, and the app only
reads its answers.
