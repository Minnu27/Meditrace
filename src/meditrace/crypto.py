"""Application-level encryption at rest for sensitive columns.

Column-level encryption only covers values that are never filtered by SQL
equality (document bytes, free-text fact values/details, filenames).
``patient_id``, ``fact_type``, ``test_or_finding`` and ``observed_date`` stay
plaintext because the app filters and groups on them in SQL; protecting those
at rest is the database/disk layer's job (see docs/SECURITY.md — MySQL
Transparent Data Encryption or volume-level encryption, plus TLS in transit).

The key is a Fernet key (AES-128-CBC + HMAC). It must be set via
``ENCRYPTION_KEY`` in any deployment that must survive a restart or run more
than one instance; a missing key is fatal outside local development, where a
throwaway key is written to a local, gitignored file for convenience.
"""

from __future__ import annotations

import os
from pathlib import Path

import json

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import LargeBinary
from sqlalchemy.dialects.mysql import LONGBLOB
from sqlalchemy.types import TypeDecorator

_DEV_KEY_FILE = Path(".meditrace_dev_key")


def _load_or_create_dev_key() -> bytes:
    if _DEV_KEY_FILE.exists():
        return _DEV_KEY_FILE.read_bytes().strip()
    key = Fernet.generate_key()
    _DEV_KEY_FILE.write_bytes(key)
    os.chmod(_DEV_KEY_FILE, 0o600)
    return key


def _resolve_key() -> bytes:
    configured = os.getenv("ENCRYPTION_KEY")
    if configured:
        return configured.encode() if isinstance(configured, str) else configured
    if os.getenv("VERCEL") == "1":
        raise RuntimeError(
            "ENCRYPTION_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"` and set it as a Vercel '
            "environment variable before deploying."
        )
    return _load_or_create_dev_key()


_fernet: Fernet | None = None


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_resolve_key())
    return _fernet


def reset_fernet_cache() -> None:
    """Used by tests that change ENCRYPTION_KEY between runs."""
    global _fernet
    _fernet = None


class EncryptedString(TypeDecorator):
    """Stores a UTF-8 string as Fernet ciphertext bytes."""

    impl = LargeBinary
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(LONGBLOB())
        return dialect.type_descriptor(LargeBinary())

    def process_bind_param(self, value: str | None, dialect) -> bytes | None:
        if value is None:
            return None
        return get_fernet().encrypt(value.encode("utf-8"))

    def process_result_value(self, value: bytes | None, dialect) -> str | None:
        if value is None:
            return None
        try:
            return get_fernet().decrypt(bytes(value)).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "Stored value could not be decrypted with the configured "
                "ENCRYPTION_KEY (wrong or rotated key?)."
            ) from exc


class EncryptedBinary(TypeDecorator):
    """Stores raw bytes (document content) as Fernet ciphertext."""

    impl = LargeBinary
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(LONGBLOB())
        return dialect.type_descriptor(LargeBinary())

    def process_bind_param(self, value: bytes | None, dialect) -> bytes | None:
        if value is None:
            return None
        return get_fernet().encrypt(value)

    def process_result_value(self, value: bytes | None, dialect) -> bytes | None:
        if value is None:
            return None
        try:
            return get_fernet().decrypt(bytes(value))
        except InvalidToken as exc:
            raise ValueError(
                "Stored object could not be decrypted with the configured "
                "ENCRYPTION_KEY (wrong or rotated key?)."
            ) from exc


class EncryptedJSON(TypeDecorator):
    """Stores a JSON-serializable dict as Fernet ciphertext bytes."""

    impl = LargeBinary
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(LONGBLOB())
        return dialect.type_descriptor(LargeBinary())

    def process_bind_param(self, value: dict | None, dialect) -> bytes | None:
        if value is None:
            return None
        return get_fernet().encrypt(json.dumps(value).encode("utf-8"))

    def process_result_value(self, value: bytes | None, dialect) -> dict | None:
        if value is None:
            return None
        try:
            return json.loads(get_fernet().decrypt(bytes(value)).decode("utf-8"))
        except InvalidToken as exc:
            raise ValueError(
                "Stored value could not be decrypted with the configured "
                "ENCRYPTION_KEY (wrong or rotated key?)."
            ) from exc


def hash_password(password: str, *, iterations: int = 600_000) -> str:
    """PBKDF2-HMAC-SHA256, OWASP-recommended iteration count, no extra deps."""
    import base64
    import hashlib

    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (
        iterations,
        base64.b64encode(salt).decode(),
        base64.b64encode(derived).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    import base64
    import hashlib
    import hmac

    try:
        scheme, iterations, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except Exception:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, int(iterations)
    )
    return hmac.compare_digest(candidate, expected)
