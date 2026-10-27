"""
Where a tenant's ClientSecret lives.

A `secret_ref` is `"<store>:<key>"` and is the only thing the database ever
holds. Swapping in Key Vault later means adding a store here and a new prefix —
no schema change, because the ref format already carries the store name.
"""

from .. import config
from .encrypted_store import EncryptedDbSecretStore
from .env_store import EnvSecretStore
from .local_file_store import LocalFileSecretStore

_STORES = {
    "env": EnvSecretStore,
    "file": LocalFileSecretStore,
    # Production: Fernet ciphertext in the app database, key from FL_SECRET_KEY.
    "encrypted": EncryptedDbSecretStore,
}


class SecretUnavailable(Exception):
    """The ref points at a store or key that is not there."""


def default_store():
    cls = _STORES.get(config.SECRET_STORE)
    if cls is None:
        raise SecretUnavailable(
            f"Unknown secret store {config.SECRET_STORE!r}. "
            f"Set FL_SECRET_STORE to one of: {', '.join(sorted(_STORES))}."
        )
    return cls()


def resolve(secret_ref: str) -> str:
    """Read the secret a ref points at. Never logged, never returned by an API."""
    store_name, _, key = (secret_ref or "").partition(":")
    cls = _STORES.get(store_name)
    if cls is None:
        raise SecretUnavailable(f"Secret reference {secret_ref!r} names an unknown store.")
    return cls().get(key)
