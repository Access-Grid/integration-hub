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

    # Metadata keys the sync engine owns and writes itself; users may not
    # set them as extra metadata. Phase 1/5 layer these on top of the
    # user-supplied extras anyway, but we reject them at save time so the
    # operator gets immediate feedback instead of a silent override.
    RESERVED_METADATA_KEYS = frozenset(
        {"pacs_credential_id", "site_code", "card_number"}
    )

    @staticmethod
    def save(
        account_id: str,
        api_secret: str,
        template_id: str,
        site_code: str = "",
        dedupe_by_site_card: bool = False,
        extra_metadata: dict[str, str] | None = None,
    ) -> None:
        _set_encrypted_json(
            AccessGridConfig.KEY,
            {
                "account_id": account_id,
                "api_secret": api_secret,
                "template_id": template_id,
                "site_code": site_code,
                "dedupe_by_site_card": bool(dedupe_by_site_card),
                "extra_metadata": dict(extra_metadata or {}),
            },
        )

    @staticmethod
    def load() -> dict[str, Any] | None:
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

    @staticmethod
    def update_dedupe(enabled: bool) -> bool:
        """Update only the dedupe_by_site_card flag. Returns True on success."""
        existing = _get_encrypted_json(AccessGridConfig.KEY)
        if not existing:
            return False
        existing["dedupe_by_site_card"] = bool(enabled)
        _set_encrypted_json(AccessGridConfig.KEY, existing)
        return True

    @staticmethod
    def update_extra_metadata(pairs: dict[str, str]) -> bool:
        """Replace the extra_metadata dict. Returns True on success."""
        existing = _get_encrypted_json(AccessGridConfig.KEY)
        if not existing:
            return False
        existing["extra_metadata"] = dict(pairs)
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
