"""
Access resolution: who can read each table, and by what path.

Four sources, combined:

1. **Workspace roles** — Admin/Member/Contributor/Viewer on the workspace grant
   access to everything in it.
2. **Item permissions** — direct shares and Build on a semantic model, from the
   Scanner API's `getArtifactUsers`.
3. **OneLake data access roles** — per-lakehouse security roles scoped to folders.
4. **Indirect access via a semantic model** — the one that matters most and that
   nobody sees in the portal: a Direct Lake model on a *fixed identity* reads
   gold tables as the model's identity, so anyone who can query the model can
   see that data whether or not they have a single permission on the table.

Group expansion turns "BI Developers" into the people inside it. Without it a
grant to one Entra group looks like one principal when it is really 142 people —
the single biggest way a permission report understates reach.
"""

import logging

from sqlalchemy.orm import Session

from ...fabric.client import FabricClient
from .. import ids, repository
from ..models import (
    KIND_LAKEHOUSE_TABLE,
    KIND_MODEL_TABLE,
    AccessGrant,
    Principal,
)

log = logging.getLogger("lineage.collect.access")

# Workspace roles that let someone read the workspace's data.
READ_ROLES = {"admin", "member", "contributor", "viewer"}

# Item permissions that imply reading the underlying data.
READ_RIGHTS = {"read", "readwrite", "build", "owner", "reshare", "explore"}


def _permission_label(role: str) -> str:
    return {
        "admin": "Admin",
        "member": "Member",
        "contributor": "Contributor",
        "viewer": "Read",
        "build": "Build",
        "read": "Read",
        "readwrite": "Write",
        "owner": "Owner",
    }.get(role.lower(), role.title() or "Read")


def collect(
    session: Session,
    client: FabricClient | None,
    *,
    customer_id: str,
    tenant_id: str,
    workspace_users: dict[str, list[dict]] | None = None,
    item_users: dict[str, list[dict]] | None = None,
    onelake_roles: dict[str, list[dict]] | None = None,
    group_members: dict[str, list[dict]] | None = None,
) -> dict:
    """
    Resolve access grants from already-collected principal data.

    Takes the Scanner API's user lists as arguments rather than fetching them,
    because the scanner collector already paid for that call — re-requesting per
    item would spend the tenant's hourly quota on data we hold.
    """
    workspace_users = workspace_users or {}
    item_users = item_users or {}
    onelake_roles = onelake_roles or {}
    group_members = group_members or {}

    errors: list[dict] = []
    counts = {"principals": 0, "grants": 0, "indirect": 0, "expanded": 0}

    # Start clean: a revoked permission must disappear, and grants are derived
    # rather than historical, so replacing them wholesale is correct here (unlike
    # delta_version, which is evidence).
    _clear_grants(session, customer_id, tenant_id)

    workspaces = {
        w.id: w for w in repository.list_workspaces(session, customer_id, tenant_id)
    }
    items = {i.id: i for i in repository.list_items(session, customer_id, tenant_id)}
    nodes = repository.list_nodes(session, customer_id, tenant_id)
    nodes_by_workspace: dict[str, list] = {}
    for node in nodes:
        if node.workspace_id:
            nodes_by_workspace.setdefault(node.workspace_id, []).append(node)

    def principal_for(entry: dict) -> Principal:
        principal_id = ids.principal_id(
            entry.get("graphId", ""), entry.get("upn", ""), tenant_id
        )
        row = session.get(Principal, principal_id)
        if row is not None and (
            row.customer_id != customer_id or row.tenant_id != tenant_id
        ):
            raise repository.TenantCollision(
                f"Principal {principal_id} belongs to another tenant; not reused."
            )
        if row is None:
            row = Principal(
                id=principal_id,
                customer_id=customer_id,
                tenant_id=tenant_id,
                graph_id=entry.get("graphId", ""),
                upn=entry.get("upn", ""),
                display_name=entry.get("name") or entry.get("upn") or "(unknown)",
                principal_type=entry.get("type", "User"),
            )
            session.add(row)
            # Flush now: every grant below carries a FK to this principal, and
            # inside one transaction SQLite enforces that per statement.
            session.flush()
            counts["principals"] += 1
        return row

    def grant(
        principal: Principal,
        *,
        node_id: str | None,
        item_id: str | None,
        permission: str,
        path: str,
        indirect: bool = False,
        note: str = "",
    ) -> None:
        session.add(
            AccessGrant(
                customer_id=customer_id,
                tenant_id=tenant_id,
                principal_id=principal.id,
                node_id=node_id,
                item_id=item_id,
                permission=permission,
                path_description=path,
                is_indirect=indirect,
                note=note,
            )
        )
        counts["grants"] += 1
        if indirect:
            counts["indirect"] += 1

    # --- 1. workspace roles ------------------------------------------------
    for workspace_id, users in workspace_users.items():
        workspace = workspaces.get(workspace_id)
        if workspace is None:
            continue
        for entry in users:
            role = (entry.get("right") or "").lower()
            if role not in READ_ROLES:
                continue
            principal = principal_for(entry)
            path = f"Workspace role · {_permission_label(role)}"
            for node in nodes_by_workspace.get(workspace_id, []):
                grant(
                    principal,
                    node_id=node.id,
                    item_id=None,
                    permission=_permission_label(role),
                    path=path,
                )
            counts["expanded"] += _expand_group(
                session,
                customer_id,
                tenant_id,
                entry,
                group_members,
                nodes_by_workspace.get(workspace_id, []),
                _permission_label(role),
                path,
                grant,
                principal_for,
            )

    # --- 2. item permissions ----------------------------------------------
    for item_id, users in item_users.items():
        item = items.get(item_id)
        if item is None:
            continue
        for entry in users:
            right = (entry.get("right") or "").lower()
            # The Scanner reports compound rights (ReadExplore,
            # ReadReshareExplore, ReadWriteReshareExplore): any Read* is a read.
            if right not in READ_RIGHTS and not right.startswith("read"):
                continue
            principal = principal_for(entry)
            grant(
                principal,
                node_id=None,
                item_id=item.id,
                permission=_permission_label(right),
                path=f"Item permission · {_permission_label(right)}",
            )

    # --- 3. OneLake data access roles -------------------------------------
    for item_id, roles in onelake_roles.items():
        item = items.get(item_id)
        if item is None:
            continue
        item_nodes = [n for n in nodes if n.item_id == item.id]
        for role in roles:
            scope = role.get("scope") or "*"
            members = role.get("members") or []
            matched = _nodes_in_scope(item_nodes, scope)
            for entry in members:
                principal = principal_for(entry)
                path = f"OneLake security role · {role.get('name', 'role')} → {scope}"
                for node in matched:
                    grant(
                        principal,
                        node_id=node.id,
                        item_id=None,
                        permission="Read",
                        path=path,
                    )
                counts["expanded"] += _expand_group(
                    session,
                    customer_id,
                    tenant_id,
                    entry,
                    group_members,
                    matched,
                    "Read",
                    path,
                    grant,
                    principal_for,
                )

    # --- 4. indirect access through a semantic model ----------------------
    counts["indirect"] += _indirect_via_models(
        session,
        customer_id,
        tenant_id,
        items=items,
        nodes=nodes,
        item_users=item_users,
        workspace_users=workspace_users,
        workspaces=workspaces,
        group_members=group_members,
        principal_for=principal_for,
        grant=grant,
    )

    session.commit()
    return {"stats": counts, "errors": errors}


def _clear_grants(session: Session, customer_id: str, tenant_id: str) -> None:
    from sqlalchemy import delete

    session.execute(
        delete(AccessGrant).where(
            AccessGrant.customer_id == customer_id,
            AccessGrant.tenant_id == tenant_id,
        )
    )
    session.commit()


def _nodes_in_scope(nodes: list, scope: str) -> list:
    """
    Which tables a OneLake role's folder scope covers.

    A scope of `sales/*` covers the sales schema; `*` or `Tables/*` covers the
    whole lakehouse. Anything narrower than a schema is treated as covering the
    schema, because a role scoped to one table's folder still grants that table.
    """
    cleaned = (scope or "*").strip().strip("/")
    if cleaned in ("", "*", "Tables", "Tables/*"):
        return list(nodes)
    cleaned = cleaned.removeprefix("Tables/")
    head = cleaned.split("/", 1)[0].rstrip("*").lower()
    if not head:
        return list(nodes)
    return [n for n in nodes if (n.schema_name or "").lower() == head]


def _expand_group(
    session,
    customer_id,
    tenant_id,
    entry: dict,
    group_members: dict,
    nodes: list,
    permission: str,
    path: str,
    grant,
    principal_for,
) -> int:
    """
    Turn a group grant into a grant per member.

    The group's own grant is kept as well: the Access tab shows "Sales Analysts
    (38 users)" as one row with the count, which is more readable than 38 rows —
    but the members are stored so a person-centred question can still be asked.
    """
    if (entry.get("type") or "").lower() != "group":
        return 0
    members = group_members.get(entry.get("graphId") or "") or []
    for member in members:
        principal = principal_for(member)
        for node in nodes:
            grant(
                principal,
                node_id=node.id,
                item_id=None,
                permission=permission,
                path=f"Member of {entry.get('name', 'group')} · {path}",
            )
    return len(members)


def _indirect_via_models(
    session,
    customer_id,
    tenant_id,
    *,
    items: dict,
    nodes: list,
    item_users: dict,
    workspace_users: dict,
    workspaces: dict,
    group_members: dict,
    principal_for,
    grant,
) -> int:
    """
    Access to a gold table granted implicitly by a semantic model.

    This is the finding the portal cannot show. A Direct Lake model configured
    with a *fixed identity* queries its source tables as that identity, so every
    principal who can query the model can read the underlying data — RLS is the
    only thing standing between them and the whole table.

    The model's upstream tables come from the lineage graph, which is why this
    runs after the definitions collector rather than alongside the scanner.
    """
    from ..graph import load_adjacency

    adjacency = load_adjacency(session, customer_id, tenant_id)
    nodes_by_id = {n.id: n for n in nodes}
    written = 0

    model_items = [i for i in items.values() if i.item_type == "SemanticModel"]
    for model in model_items:
        if not model.uses_fixed_identity:
            # Without a fixed identity the model passes the user's own identity
            # through, so table permissions still apply and there is nothing
            # indirect to report.
            continue

        model_nodes = [n for n in nodes if n.item_id == model.id]
        upstream: set[str] = set()
        for node in model_nodes:
            # ONE hop, not the transitive closure. A Direct Lake partition reads
            # the specific table it names; being able to query the model does not
            # grant you that table's own sources. Walking further would claim
            # every bronze table and external system is exposed through the
            # report, which is both wrong and alarming.
            upstream |= set(adjacency.walk(node.id, "up", 1))
        tables = [
            nodes_by_id[node_id]
            for node_id in upstream
            if node_id in nodes_by_id
            and nodes_by_id[node_id].kind in (KIND_LAKEHOUSE_TABLE, KIND_MODEL_TABLE)
            and nodes_by_id[node_id].item_id != model.id
        ]
        if not tables:
            continue

        readers = list(item_users.get(model.id) or [])
        workspace = workspaces.get(model.workspace_id)
        if workspace is not None:
            readers.extend(workspace_users.get(model.workspace_id) or [])

        note = "Reads via model identity, RLS applies"
        path = (
            f"{workspace.name if workspace else ''} › "
            f"{model.display_name} ({model.storage_mode or 'Direct Lake'}, "
            f"fixed identity)"
        ).strip(" ›")

        for entry in readers:
            right = (entry.get("right") or "").lower()
            if (
                right not in READ_RIGHTS
                and right not in READ_ROLES
                and not right.startswith("read")
            ):
                continue

            # The group itself AND its members. Without expanding here, an app
            # audience of 142 people reports as one principal — which understates
            # the reach of exactly the finding this rule exists to surface.
            recipients = [(entry, path)]
            if (entry.get("type") or "").lower() == "group":
                group_name = entry.get("name", "group")
                recipients.extend(
                    (member, f"Member of {group_name} · {path}")
                    for member in group_members.get(entry.get("graphId") or "") or []
                )

            for who, how in recipients:
                principal = principal_for(who)
                for table in tables:
                    grant(
                        principal,
                        node_id=table.id,
                        item_id=None,
                        permission="Read (indirect)",
                        path=how,
                        indirect=True,
                        note=note,
                    )
                    written += 1
    return written


def summarise_for_node(
    session: Session, customer_id: str, tenant_id: str, node_id: str
) -> dict:
    """
    The Access tab's summary sentence plus its rows.

    The sentence is the point: "151 principals can see this data: 9 directly,
    142 indirectly through the semantic model" is the answer to the question a
    customer actually asked.
    """
    from sqlalchemy import select

    stmt = (
        select(AccessGrant, Principal)
        .join(Principal, Principal.id == AccessGrant.principal_id)
        .where(
            AccessGrant.customer_id == customer_id,
            AccessGrant.tenant_id == tenant_id,
            AccessGrant.node_id == node_id,
        )
    )
    rows = list(session.execute(stmt))

    # Deduplicate by (principal, path): one principal reachable by two routes is
    # two rows, but the same route twice is not.
    seen: set[tuple[str, str]] = set()
    entries: list[dict] = []
    direct_principals: set[str] = set()
    indirect_principals: set[str] = set()

    for gr, principal in rows:
        key = (principal.id, gr.path_description)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "principal": principal.display_name,
                "principalType": principal.principal_type,
                "upn": principal.upn,
                "path": gr.path_description,
                "permission": gr.permission,
                "isIndirect": gr.is_indirect,
                "note": gr.note,
            }
        )
        if gr.is_indirect:
            indirect_principals.add(principal.id)
        else:
            direct_principals.add(principal.id)

    total = len(direct_principals | indirect_principals)
    only_indirect = indirect_principals - direct_principals
    if total == 0:
        summary = "No resolved access. Run the access collector to populate this."
    elif only_indirect:
        summary = (
            f"{total} principals can see this data: {len(direct_principals)} "
            f"directly, {len(only_indirect)} indirectly through a semantic model."
        )
    else:
        summary = f"{total} principals can read this table."

    entries.sort(key=lambda e: (e["isIndirect"], e["principal"].lower()))
    return {"summary": summary, "rows": entries}
