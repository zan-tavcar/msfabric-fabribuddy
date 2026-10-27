# Troubleshooting

| Symptom | Fix |
|---|---|
| **Deploy:** `POST /workspaces/…/sqlDatabases failed (403)` | You need the **Admin** or **Member** role on the workspace. |
| **Deploy:** `FabriBuddy_DB isn't ready yet` | A new SQL database takes a minute to provision. Run the notebook again. |
| **Scan:** `Set key_vault_url, sp_tenant_id, sp_client_id in the parameters of Load_FabriBuddy_E2E` | Fill them in the pipeline's [settings](settings.md). |
| **Scan:** a Key Vault error reading the secret | Whoever runs the pipeline (the schedule's owner) needs **Key Vault Secrets User** on the vault. Check `sp_secret_name`. |
| **Scan:** `Service principal sign-in failed` or `Could not reach the sign-in authority` | Check `sp_tenant_id`, `sp_client_id` and the secret's value in Key Vault. |
| **Scan:** `The scan could not start: …` | The *read-only admin APIs* tenant setting isn't on for the principal's group yet. It takes up to 15 minutes. |
| **Scan:** `No SQL database called 'FabriBuddy_DB'` | Run **Deploy_FabriBuddy** first, or set `sql_database_name` to your database's name. |
| **Scan:** `fabribuddy_core was already imported in this session` | Stop the session, then run again. |
| **Scan:** `pip` can't download packages | Your tenant blocks outbound access from notebooks. Allow PyPI (`pypi.org`, `files.pythonhosted.org`). |
| **App:** *Not in the last scan* | The scan hasn't finished since the app was added. Wait for it, then reload. |
| **App:** *GraphQL errors: No schema document exists* | The connector wasn't applied. In `app/`, run `npx rayfin up connector apply`, then reload. |
| **App:** *Sign in to continue* | You opened the app by its address. Open it from the workspace instead, or sign in once. |
| **App:** `rayfin up` asks for a workspace | Pass `--workspace-id <workspace-id>`. |
| **Licences** shows as unavailable | Add the optional [Microsoft Graph permissions](service-principal.md#2-microsoft-graph-permissions-optional). |
| Lineage stops at a table | The code writing it is in a workspace that isn't scanned, or where the principal isn't Contributor. |

Still stuck? [Open an issue](https://github.com/zan-tavcar/msfabric-fabribuddy/issues) with the
scan's output. It never contains secrets.
