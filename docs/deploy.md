# Deploy FabriBuddy

About 15 minutes of work, plus waiting for the first scan. You import one notebook and run it. It
creates everything else, then prints the commands that add the app.

## Before you start

| You need | Why |
|---|---|
| A workspace on a **Fabric capacity** (F SKU or trial), where you're **Admin** | FabriBuddy's items live there. Fabric Apps don't run on Pro or PPU. A new workspace keeps sharing simple. |
| A **service principal**, with its secret in **Azure Key Vault** | The scan reads your tenant as this principal. [Set it up →](service-principal.md) |
| **Key Vault Secrets User** on that vault, for you | The scan reads the secret as whoever runs the pipeline, or owns its schedule. |
| **Fabric Apps (preview)** turned on | *Admin portal → Tenant settings → Fabric Apps (preview)*. Needed for the app only. |
| **Node.js 20 or later** on your computer | To add the app, once. |

## 1. Run the deploy notebook

1. Download [`scripts/Deploy_FabriBuddy.ipynb`](https://github.com/zan-tavcar/msfabric-fabribuddy/raw/main/scripts/Deploy_FabriBuddy.ipynb).
2. In your workspace: *Import → Notebook → From this computer*, and choose the file.
3. Open **Deploy_FabriBuddy**. Check that the language at the top is **Python** (not PySpark).
4. Fill in the first code cell:

    ```python
    KEY_VAULT_URL = "https://<your-vault>.vault.azure.net/"
    SP_TENANT_ID = "<directory (tenant) id>"
    SP_CLIENT_ID = "<application (client) id>"
    SP_SECRET_NAME = "fabribuddy-sp-secret"   # the secret's name in Key Vault
    ```

    The other settings are optional:

    | Setting | Default | |
    |---|---|---|
    | `WORKSPACES` | `""` | Workspace IDs to scan, comma-separated. Empty means every workspace. |
    | `INCLUDE_PERSONAL` | `False` | Report Audience: include people's personal *My workspace*. |
    | `TENANT_NAME` | `""` | How the app names your tenant, for example `"Contoso"`. |
    | `SCHEDULE_DAILY_AT_UTC` | `"05:00"` | The daily scan time, in UTC. `""` means no schedule. |
    | `RUN_AFTER_DEPLOY` | `True` | Start the first scan when deployment finishes. |

5. Choose **Run all**.

The notebook prints each step:

```text
FabriBuddy 0.1.0 from zan-tavcar/msfabric-fabribuddy@main
✓ FabriBuddy_DB
✓ FabriBuddy 0.1.0 stored in FabriBuddy_DB
✓ FabriBuddy_Scan.Notebook created
✓ Check_FabriBuddy_Version.Notebook created
✓ Load_FabriBuddy_E2E.DataPipeline created
✓ Scan: Load_FabriBuddy_E2E, FabriBuddy_Scan
✓ Storage: FabriBuddy_DB
✓ Maintenance: Check_FabriBuddy_Version, Deploy_FabriBuddy
✓ Schedule: Load_FabriBuddy_E2E daily at 05:00 UTC
✓ First scan started: open Load_FabriBuddy_E2E → View run history (10–30 minutes).
```

Your workspace now has three folders:

| Folder | Items |
|---|---|
| **Scan** | `Load_FabriBuddy_E2E` (pipeline, scheduled) and `FabriBuddy_Scan` (notebook) |
| **Storage** | `FabriBuddy_DB` (SQL database) |
| **Maintenance** | `Check_FabriBuddy_Version` and `Deploy_FabriBuddy` (notebooks) |

!!! tip "Try it without a service principal"
    To look at the app before setting anything up, deploy with the service principal left empty
    and `RUN_AFTER_DEPLOY = False`. Then run **Load_FabriBuddy_E2E** with the parameter
    `demo` set to `true`. It loads the fictional Contoso tenant instead of scanning yours.

## 2. Add the app { #add-the-app }

The deploy notebook's last cell prints these commands, with your workspace and database IDs
already in them. Run them on your computer:

```bash
git clone https://github.com/zan-tavcar/msfabric-fabribuddy.git
cd msfabric-fabribuddy/app
npm install
npx rayfin login                         # sign in with your Fabric account
npm run connect -- <workspace-id> <sql-database-id>
npx rayfin up --workspace-id <workspace-id>
```

- **`npm run connect`** points the app at your `FabriBuddy_DB`.
- **`npx rayfin up`** creates the **fabribuddy** app in your workspace, builds it and uploads it.
  It takes a minute or two and prints the app's address at the end.

More on the app, including sharing, in [The app](app.md).

## 3. Open it

Open **fabribuddy** in the workspace. Fabric signs you in. Until the first scan has finished, the
pages say *Not in the last scan*; reload once it has.

## Next

- [Share the app](app.md#sharing)
- [Change what's scanned](settings.md)
- [Update to a new version](update.md)
- [Troubleshooting](troubleshooting.md)
