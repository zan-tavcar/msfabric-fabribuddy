# Settings

FabriBuddy's settings are the **parameters of the pipeline Load_FabriBuddy_E2E**. The deploy
notebook fills them in the first time, and keeps them on every update.

To change them, open **Load_FabriBuddy_E2E**, select the background of the canvas, open
**Parameters**, change the default values and **Save**. The next run uses them. To see the result
now, choose **Run**.

| Parameter | Default | |
|---|---|---|
| `key_vault_url` | | The Azure Key Vault that holds the service principal's secret. |
| `sp_tenant_id` | | The directory (tenant) ID. |
| `sp_client_id` | | The service principal's application (client) ID. |
| `sp_secret_name` | `fabribuddy-sp-secret` | The secret's name in Key Vault. |
| `workspaces` | | Workspace IDs to scan, comma-separated. Empty means every workspace. |
| `include_personal` | `false` | Report Audience: include people's personal *My workspace*. |
| `tenant_name` | | How the app names your tenant. |
| `full_refresh` | `false` | Re-read everything instead of only what changed since the last scan. |
| `demo` | `false` | Load the fictional Contoso tenant instead of scanning yours. |
| `sql_database_name` | `FabriBuddy_DB` | The database the scan writes to. |

## Choosing workspaces

Pick whole chains: the workspaces where notebooks and pipelines run, the lakehouses and warehouses
they write into, and the workspaces holding the reports on top. Lineage stops at the edge of what's
scanned, and a table written from a workspace you didn't pick shows as an unresolved target.

## The schedule

The deploy notebook schedules the pipeline daily. Change it in **Load_FabriBuddy_E2E → Schedule**.
Later scans re-read only what changed, so running more often costs little.

Fabric expires a schedule whose owner hasn't signed in for 90 days.
