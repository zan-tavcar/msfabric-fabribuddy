"""
Secrets in a local JSON file, for development.

Not production storage — the file is plaintext on disk. It exists so a
developer can connect a real tenant without exporting env vars, and CI fails
the build if one is ever committed (scripts/check_secrets.py).
"""

import json
import os
import uuid
from pathlib import Path

from .. import config


class LocalFileSecretStore:
    name = "file"

    def __init__(self, path: str | None = None):
        self.path = Path(path or config.SECRET_FILE)

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt store must not take the app down — the tenant simply
            # reports as needing reconnection.
            return {}

    def put(self, key: str, value: str) -> str:
        key = key or str(uuid.uuid4())
        data = self._read()
        data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 0600 before the write, not after: a world-readable window is still a
        # leak even if it only lasts a millisecond.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        return key

    def get(self, key: str) -> str:
        from . import SecretUnavailable

        value = self._read().get(key)
        if not value:
            raise SecretUnavailable(
                "This tenant's stored secret is missing. Reconnect the tenant."
            )
        return value

    def delete(self, key: str) -> None:
        data = self._read()
        if data.pop(key, None) is not None:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
