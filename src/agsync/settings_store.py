"""Persistent app settings stored in the SQLite `settings` table.

Provides typed helpers for the few well-known keys (AccessGrid creds,
chosen PACS, PACS creds) and a generic get/set for the rest.

Secret values are encrypted at write time via crypto.encrypt and
decrypted on read. Plain values (e.g. chosen PACS vendor) are stored raw.
"""

from __future__ import annotations

import json
from typing import Any

from .crypto import decrypt, encrypt
from .db.connection import execute_one, get_db

# ---- generic key/value -----------------------------------------------------

def get(key: str, default: str | None = None) -> str | None:
    row = execute_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set(key: str, value: str) -> None:  # noqa: A001 — intentional shadow
    get_db().execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def delete(key: str) -> None:
    get_db().execute("DELETE FROM settings WHERE key = ?", (key,))


# ---- encrypted JSON blob helpers ------------------------------------------

def _set_encrypted_json(key: str, payload: dict[str, Any]) -> None:
    set(key, encrypt(json.dumps(payload)))


def _get_encrypted_json(key: str) -> dict[str, Any] | None:
    raw = get(key)
    if not raw:
        return None
    return json.loads(decrypt(raw))


# ---- well-known keys -------------------------------------------------------

class AccessGridConfig:
    KEY = "accessgrid"

    @staticmethod
    def save(
        account_id: str,
        api_secret: str,
        template_id: str,
        site_code: str = "",
    ) -> None:
        _set_encrypted_json(
            AccessGridConfig.KEY,
            {
                "account_id": account_id,
                "api_secret": api_secret,
                "template_id": template_id,
                "site_code": site_code,
            },
        )

    @staticmethod
    def load() -> dict[str, str] | None:
        return _get_encrypted_json(AccessGridConfig.KEY)

    @staticmethod
    def update_site_code(site_code: str) -> bool:
        """Update only the site_code on an existing config. Returns True on success."""
        existing = _get_encrypted_json(AccessGridConfig.KEY)
        if not existing:
            return False
        existing["site_code"] = site_code
        _set_encrypted_json(AccessGridConfig.KEY, existing)
        return True


class PacsConfig:
    KEY = "pacs"

    @staticmethod
    def save(vendor: str, params: dict[str, Any]) -> None:
        _set_encrypted_json(PacsConfig.KEY, {"vendor": vendor, "params": params})

    @staticmethod
    def load() -> dict[str, Any] | None:
        return _get_encrypted_json(PacsConfig.KEY)


def is_configured() -> bool:
    return AccessGridConfig.load() is not None and PacsConfig.load() is not None
