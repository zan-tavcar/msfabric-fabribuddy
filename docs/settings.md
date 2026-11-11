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

## Live Job Monitor { #live-job-monitor }

By default the [Job Monitor](job-monitor.md) shows the runs the daily scan read. To see runs as
they happen, the **FabriBuddy_Live** notebook (in the *Scan* folder) refreshes only the monitor's
pages in `FabriBuddy_DB`, every minute.

There are two refresh intervals:

| | Every | |
|---|---|---|
| **FabriBuddy_Live** reads Fabric | 60 seconds | It reads what's running, what those runs start next, and what a schedule is about to start, plus a rotating slice of everything else. |
| **The app** reads `FabriBuddy_DB` | 30 seconds | Always, on every Job Monitor page. With the live notebook off, it just finds nothing new until the next scan. |

So a new run or a finished step shows in the app within about a minute and a half. The badge at
the top of the monitor says **Live**, **Live paused** (the notebook stopped) or **Last scan**
(live is off).

**Turn it on:**

- **When you deploy or update:** set `LIVE_MONITOR = True` in **Deploy_FabriBuddy** and run it.
  It schedules **FabriBuddy_Live** every hour, and each run stays live for 55 minutes. An existing
  schedule is left as it is.
- **Or yourself:** open **FabriBuddy_Live → Schedule** and add an hourly schedule, or run it by
  hand for an hour of live data.

**Turn it off:** open **FabriBuddy_Live → Schedule** and turn the schedule off or delete it.
Running Deploy_FabriBuddy with `LIVE_MONITOR = False` doesn't remove it.

Its own parameters, in the notebook's first code cell:

| Parameter | Default | |
|---|---|---|
| `minutes` | `55` | How long one run keeps the monitor live. Keep it under the schedule's interval. |
| `every_seconds` | `60` | How often it reads Fabric. Lower means fresher, and more API calls. |
| `sql_database_name` | `FabriBuddy_DB` | The database it writes to. |

Everything else (the service principal, the workspaces) comes from **Load_FabriBuddy_E2E**'s
parameters, and it needs one scan to have run first.

**Cost:** a small Python notebook session for as long as it runs, so all day when it's scheduled
hourly. It's read-only, like the scan: GET calls only, plus Fabric's read-only
`queryactivityruns`.

## Choosing workspaces

Pick whole chains: the workspaces where notebooks and pipelines run, the lakehouses and warehouses
they write into, and the workspaces holding the reports on top. Lineage stops at the edge of what's
scanned, and a table written from a workspace you didn't pick shows as an unresolved target.

## The schedule

The deploy notebook schedules the pipeline daily. Change it in **Load_FabriBuddy_E2E → Schedule**.
Later scans re-read only what changed, so running more often costs little.

Fabric expires a schedule whose owner hasn't signed in for 90 days.
