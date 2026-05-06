"""Symmetric encryption helpers for secrets at rest.

Uses Fernet (AES-128-CBC + HMAC) with a key supplied via the
AG_SYNC_ENCRYPTION_KEY environment variable. If the key is lost, the
existing database is unrecoverable — that's the trade-off for portability.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


class CryptoError(RuntimeError):
    """Raised when encryption or decryption fails."""


def _fernet() -> Fernet:
    key = get_settings().encryption_key.encode()
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise CryptoError(
            "AG_SYNC_ENCRYPTION_KEY is not a valid Fernet key. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        ) from exc


def encrypt(plaintext: str) -> str:
    if plaintext is None:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise CryptoError(
            "Failed to decrypt secret — the encryption key has changed since this "
            "value was written. Run 'agsync reset-admin' and re-run the wizard."
        ) from exc
