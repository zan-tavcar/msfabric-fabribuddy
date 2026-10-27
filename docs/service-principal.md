# Service principal

The scan reads your tenant as a **service principal**: an app registration that your admins create
and control, in your own Entra ID. FabriBuddy never sees its secret. The secret stays in your Azure
Key Vault, and the scan reads it into memory at the start of each run.

About 15 minutes of work, plus up to 15 minutes for a tenant setting to apply.

## 1. Create the app registration

In *Entra ID → App registrations → New registration*, register an app with any name, for example
`fabribuddy`. It needs no redirect URI.

Note the **Application (client) ID** and the **Directory (tenant) ID** from its overview. Under
*Certificates & secrets*, create a **client secret** and copy its value.

!!! warning "Don't add Power BI Service permissions"
    The admin APIs refuse service principals that have Power BI Service API permissions.

## 2. Microsoft Graph permissions (optional)

Only Report Audience uses Microsoft Graph, for two things:

| Permission (application) | Used for |
|---|---|
| `User.Read.All`, `Organization.Read.All` | Licences: who holds a Pro or PPU seat |
| `GroupMember.Read.All` | The people inside a group that has access to a report, nested groups included |

Grant admin consent. Without these, the Licences page says it's unavailable, and access granted to a
group shows as the group rather than its people.

## 3. Allow the read-only admin APIs

In the Fabric *Admin portal → Tenant settings*:

1. Put the service principal in a security group, for example `sg-fabribuddy`.
2. Enable **Service principals can use read-only admin APIs** for that group.

This is required for both the Data Catalog and Report Audience. The *Enhance admin API responses
with detailed metadata* settings aren't needed. Tenant settings take up to 15 minutes to apply.

## 4. Contributor on the workspaces to document

For the Data Catalog, add the principal as **Contributor** on each workspace you want documented:
*Workspace → Manage access → Add people or groups*. Include the workspaces your code writes into,
not only the ones that hold the code. Report Audience needs no workspace role.

The principal needs **no role on the FabriBuddy workspace**: whoever runs the pipeline writes
`FabriBuddy_DB` with their own identity.

### Why Contributor? { #why-contributor }

Fabric's `getDefinition` API returns the code of a notebook, pipeline or dataflow. It requires the
caller to be able to write the item, even though it only reads. FabriBuddy never writes: its client
refuses every request except reads.

Without Contributor on a workspace you still get its tables, Delta history, access and Report
Audience. You don't get its code, so it has no lineage.

## 5. Store the secret in Key Vault

1. In an Azure Key Vault, add the client secret as a secret, for example `fabribuddy-sp-secret`.
2. Give the **Key Vault Secrets User** role on the vault to whoever runs the scan. A scheduled
   pipeline runs as the schedule's owner, which is you if you ran the deploy notebook.

Then fill in `KEY_VAULT_URL`, `SP_TENANT_ID`, `SP_CLIENT_ID` and `SP_SECRET_NAME` in the
[deploy notebook](deploy.md). After deployment, they live in the pipeline's
[parameters](settings.md).

## Revoking access

Delete the client secret or the app registration in Entra ID, or remove the principal from the
security group or the workspaces. The next scan fails to sign in and changes nothing.
