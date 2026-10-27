# Your data stays in your tenant

<div class="fb-promise" markdown>
**FabriBuddy is a Fabric App hosted in your own Fabric tenant. No data leaves your tenant.**

Everything FabriBuddy is made of (the scan notebook, the pipeline, the SQL database and the app)
lives in a workspace you own and runs on your own capacity. Nothing it reads is sent to
FabriBuddy, to its author or to anyone else. There is no FabriBuddy server to send it to.
</div>

This page explains what that means in practice, so your security team can check it.

## Where everything runs

| Part | What it is | Where it runs |
|---|---|---|
| **Load_FabriBuddy_E2E** | A Fabric data pipeline that starts the scan on a schedule | Your workspace |
| **FabriBuddy_Scan** | A Python notebook that reads your tenant | Your workspace, on your capacity |
| **FabriBuddy_DB** | A Fabric SQL database that stores what the scan found | Your workspace |
| **fabribuddy** | A Fabric App, the interface | Your workspace, served by Fabric |

The app has no back end of its own. Each page is one read of `FabriBuddy_DB`, made **as the person
viewing it** with their own Fabric sign-in. Someone who can't read the database can't see anything
in the app.

## No account, no telemetry

- **No sign-up.** People sign in with their Fabric account, as they do for any Fabric item.
- **No analytics or tracking** in the scanner or in the app.
- **No licence key and no call home.** FabriBuddy is free and works the same without any connection
  to its author.

## What it reads

| Read | Never read |
|---|---|
| Workspaces, items and their definitions (the code of notebooks, pipelines, dataflows, semantic models, reports) | The rows in your tables |
| Table schemas, Delta commit history, job runs | The data in your reports and semantic models |
| Access: workspace roles, item permissions, OneLake roles, Entra group members | Files, mail or anything outside Fabric and Power BI |
| The Power BI audit log: who opened what, and when | |
| Pro and PPU licence assignments (optional, through Microsoft Graph) | |

## Read-only

FabriBuddy never creates, changes or deletes anything in the tenant it scans. Every call it makes
to Microsoft is a `GET`, or one of the few `POST` operations that only read (`getDefinition`, and
the admin scanner's `getInfo`). Its Fabric client
([`client.py`](https://github.com/zan-tavcar/msfabric-fabribuddy/blob/main/core/src/fabribuddy_core/fabric/client.py))
refuses any other request before it's sent, and
[a test](https://github.com/zan-tavcar/msfabric-fabribuddy/blob/main/core/tests/test_read_only.py)
checks that. The warehouse SQL endpoints only receive fixed metadata queries.

The only thing FabriBuddy writes to is **its own database, `FabriBuddy_DB`**.

The one permission that sounds otherwise is **Contributor** on the workspaces you want documented.
Microsoft's API for reading a notebook's or pipeline's code (`getDefinition`) requires it, even for
reading. See [Service principal → Why Contributor?](service-principal.md#why-contributor)

## Every network call it makes

All of these are outbound requests from your workspace. None of them carries data from your
tenant to anyone other than Microsoft's own services for your tenant.

| Who calls | Where | Why |
|---|---|---|
| FabriBuddy_Scan | `api.fabric.microsoft.com`, `api.powerbi.com`, `onelake.dfs.fabric.microsoft.com`, your warehouse SQL endpoints | Reading your tenant's metadata, code and access |
| FabriBuddy_Scan | `login.microsoftonline.com`, your Azure Key Vault | Signing in as your service principal |
| FabriBuddy_Scan | `graph.microsoft.com` (optional) | Licences and Entra group members |
| FabriBuddy_Scan | PyPI (`pypi.org`, `files.pythonhosted.org`) | Installing the open-source Python packages FabriBuddy uses. Nothing is uploaded. |
| Deploy_FabriBuddy, Check_FabriBuddy_Version | `github.com`, `api.github.com`, `raw.githubusercontent.com` | Downloading the public FabriBuddy release, and checking for a newer one. Nothing is uploaded. |

Once deployed, the release lives in `FabriBuddy_DB`, so a scan needs neither GitHub nor anything
else outside Microsoft.

## Who can see the results

FabriBuddy's results are as private as its workspace. Someone can open the app only if you give
them access to the app **and** read access to `FabriBuddy_DB`. See
[The app → Sharing](app.md#sharing).

Everyone who can open the app sees everything in it: the whole scanned estate, and who can see
each report. Share it with the people who would see that in the Fabric admin portal anyway.

## Removing it

Delete the FabriBuddy items from the workspace, or the workspace itself, and everything FabriBuddy
collected goes with it. To revoke its access, delete the service principal's secret or its app
registration in Entra ID.
