"""The three values that identify a tenant's service principal."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Credentials:
    """
    Passed explicitly everywhere rather than read from a global, so one process
    can serve several connected tenants without their credentials ever being
    ambiguous about which tenant they belong to.
    """

    tenant_id: str
    client_id: str
    client_secret: str

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # A dataclass repr would print the secret into any traceback that
        # happens to carry a Credentials instance.
        return f"Credentials(tenant_id={self.tenant_id!r}, client_id={self.client_id!r})"
