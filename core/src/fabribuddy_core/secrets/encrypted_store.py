"""
Secrets encrypted in the application database — the production store.

Fernet (AES-128-CBC + HMAC-SHA256) with a key from FL_SECRET_KEY, which lives
in the host's secret settings (App Service / Container Apps secret, Key Vault
reference), never in the database. The database alone therefore never yields a
client secret, and container restarts lose nothing, unlike the local file store.
"""

import os
import uuid

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .. import config
from ..db import Base, SessionLocal


class SecretBlob(Base):
    __tablename__ = "secret_blob"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)


def _fernet():
    from cryptography.fernet import Fernet

    from . import SecretUnavailable

    key = os.environ.get(config.SECRET_KEY_ENV, "")
    if not key:
        raise SecretUnavailable(
            f"{config.SECRET_KEY_ENV} is not set — the encrypted secret store needs it."
        )
    try:
        return Fernet(key.encode())
    except ValueError:
        raise SecretUnavailable(
            f"{config.SECRET_KEY_ENV} is not a valid Fernet key (32 url-safe base64 bytes)."
        ) from None


class EncryptedDbSecretStore:
    name = "encrypted"

    def put(self, key: str, value: str) -> str:
        key = key or str(uuid.uuid4())
        token = _fernet().encrypt(value.encode()).decode()
        with SessionLocal() as session:
            row = session.get(SecretBlob, key)
            if row is None:
                session.add(SecretBlob(key=key, ciphertext=token))
            else:
                row.ciphertext = token
            session.commit()
        return key

    def get(self, key: str) -> str:
        from cryptography.fernet import InvalidToken

        from . import SecretUnavailable

        with SessionLocal() as session:
            row = session.get(SecretBlob, key)
        if row is None:
            raise SecretUnavailable(
                "This tenant's stored secret is missing. Reconnect the tenant."
            )
        try:
            return _fernet().decrypt(row.ciphertext.encode()).decode()
        except InvalidToken:
            raise SecretUnavailable(
                f"The stored secret cannot be decrypted — was {config.SECRET_KEY_ENV} rotated? "
                "Re-enter the client secret on the Admin page."
            ) from None

    def delete(self, key: str) -> None:
        with SessionLocal() as session:
            row = session.get(SecretBlob, key)
            if row is not None:
                session.delete(row)
                session.commit()
