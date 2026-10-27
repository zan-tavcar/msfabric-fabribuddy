"""Secrets from environment variables. Read-only: the process cannot add one."""

import os


class EnvSecretStore:
    name = "env"

    def put(self, key: str, value: str) -> str:
        raise NotImplementedError(
            "The env secret store is read-only. Set FL_SECRET_STORE=file to "
            "store a secret locally, or supply the value via the environment."
        )

    def get(self, key: str) -> str:
        from . import SecretUnavailable

        value = os.environ.get(key)
        if not value:
            raise SecretUnavailable(
                f"Environment variable {key} is not set, so this tenant's "
                f"credentials cannot be read."
            )
        return value
