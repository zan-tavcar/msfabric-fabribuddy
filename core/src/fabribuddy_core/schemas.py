"""
Request/response models.

JSON is camelCase so the TypeScript client can consume payloads without a
mapping layer. No response model carries a ClientSecret — the request model is
the only place it appears, as a SecretStr so it cannot be printed or logged by
accident.
"""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic.alias_generators import to_camel


class _Base(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ConnectTenantRequest(_Base):
    tenant_id: str = Field(min_length=1, description="Azure/Entra tenant GUID")
    client_id: str = Field(min_length=1, description="App registration client ID")
    client_secret: SecretStr = Field(min_length=1, description="SPN client secret value")
    display_name: str = Field(default="", max_length=200)

    @field_validator("tenant_id", "client_id", "display_name", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v


class ContributorWorkspacesRequest(_Base):
    """Which workspaces the customer opts into Contributor-tier collection for."""

    workspace_ids: list[str] = Field(default_factory=list)


class TenantOut(_Base):
    id: str
    display_name: str
    tenant_id: str = Field(validation_alias="azure_tenant_id")
    client_id: str
    status: str
    capabilities: dict = {}
    contributor_workspaces: list[str] = []
    is_demo: bool = False
    # The store the secret lives in ('local' or 'env') — never the secret, and
    # never the full ref, which would name the exact env var.
    secret_store: str = ""
    last_validated_at: dt.datetime | None = None
    last_validation_error: str | None = None
    created_at: dt.datetime | None = None

    @classmethod
    def of(cls, tenant) -> "TenantOut":
        ref = tenant.secret_ref or ""
        return cls(
            id=tenant.id,
            display_name=tenant.display_name,
            azure_tenant_id=tenant.azure_tenant_id,
            client_id=tenant.client_id,
            status=tenant.status,
            capabilities=tenant.capabilities or {},
            contributor_workspaces=tenant.contributor_workspaces or [],
            is_demo=bool(tenant.is_demo),
            secret_store=ref.split(":", 1)[0] if ":" in ref else "",
            last_validated_at=tenant.last_validated_at,
            last_validation_error=tenant.last_validation_error,
            created_at=tenant.created_at,
        )


class CollectorRunOut(_Base):
    id: str
    kind: str
    status: str
    error: str | None = None
    params: dict = {}
    stats: dict = {}
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None

    @classmethod
    def of(cls, run) -> "CollectorRunOut":
        return cls(
            id=run.id,
            kind=run.kind,
            status=run.status,
            error=run.error,
            params=run.params or {},
            stats=run.stats or {},
            started_at=run.started_at,
            finished_at=run.finished_at,
        )


class CollectorErrorOut(_Base):
    collector: str
    scope: str
    scope_id: str
    scope_name: str
    reason: str
    message: str
    occurred_at: dt.datetime | None = None

    @classmethod
    def of(cls, row) -> "CollectorErrorOut":
        return cls(
            collector=row.collector,
            scope=row.scope,
            scope_id=row.scope_id,
            scope_name=row.scope_name,
            reason=row.reason,
            message=row.message,
            occurred_at=row.occurred_at,
        )


class ScanRunOut(_Base):
    """One Audience scan run (`audience_scan_run`), without its payload."""

    id: str
    kind: str
    status: str
    error: str | None = None
    params: dict = {}
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None

    @classmethod
    def of(cls, run) -> "ScanRunOut":
        return cls(
            id=run.id,
            kind=run.kind,
            status=run.status,
            error=run.error,
            params=run.params or {},
            started_at=run.started_at,
            finished_at=run.finished_at,
        )
