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


def set_json(key: str, payload: Any) -> None:
    """Store an arbitrary encrypted JSON blob under `key`."""
    set(key, encrypt(json.dumps(payload)))


def get_json(key: str) -> Any:
    """Read back a blob written by set_json, or None."""
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

    # Sent on every pass this install issues. They describe the deployment
    # rather than the person — a PACS that has no job titles still wants its
    # passes labelled consistently — so they are configured once here instead
    # of being read per-cardholder.
    DEFAULT_CARD_CLASSIFICATION = "Resident"

    @staticmethod
    def save(
        account_id: str,
        api_secret: str,
        template_id: str,
        site_code: str = "",
        dedupe_by_site_card: bool = False,
        extra_metadata: dict[str, str] | None = None,
        card_title: str = "",
        card_classification: str = DEFAULT_CARD_CLASSIFICATION,
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
                "card_title": card_title,
                "card_classification": card_classification,
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
    def update_card_fields(card_title: str, card_classification: str) -> bool:
        """Update the title and classification stamped on every pass."""
        existing = _get_encrypted_json(AccessGridConfig.KEY)
        if not existing:
            return False
        existing["card_title"] = card_title
        existing["card_classification"] = card_classification
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

    # How a credential's identity is transmitted to AccessGrid.
    ENCODING_SITE_CARD = "site_card"  # default: decoded site_code + card_number
    ENCODING_FILE_DATA = "file_data"  # verbatim pre-encoded payload

    @staticmethod
    def save(vendor: str, params: dict[str, Any], options: dict[str, Any] | None = None) -> None:
        _set_encrypted_json(
            PacsConfig.KEY,
            {"vendor": vendor, "params": params, "options": dict(options or {})},
        )

    @staticmethod
    def load() -> dict[str, Any] | None:
        return _get_encrypted_json(PacsConfig.KEY)

    @staticmethod
    def update_params(**changes: Any) -> bool:
        """Merge changes into the saved connection params.

        Merge rather than replace: the params dict also holds values the
        connect flow wrote (the enrollment trigger), and a settings form that
        only knows about two fields must not drop the rest.
        """
        existing = _get_encrypted_json(PacsConfig.KEY)
        if not existing:
            return False
        params = dict(existing.get("params") or {})
        params.update(changes)
        existing["params"] = params
        _set_encrypted_json(PacsConfig.KEY, existing)
        return True

    @staticmethod
    def credential_encoding() -> str:
        """Return the configured encoding, defaulting to site_card."""
        cfg = _get_encrypted_json(PacsConfig.KEY) or {}
        enc = (cfg.get("options") or {}).get("credential_encoding")
        return enc if enc == PacsConfig.ENCODING_FILE_DATA else PacsConfig.ENCODING_SITE_CARD


class NotificationConfig:
    """Optional SMTP relay for operator notifications.

    Optional on purpose: the only notification that exists is the "sign in
    again" nag, and an install with no relay still shows it in the UI and
    the logs. Nothing here gates syncing.
    """

    KEY = "notifications"

    @staticmethod
    def save(
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_username: str = "",
        smtp_password: str = "",
        from_address: str = "",
        use_starttls: bool = True,
        use_ssl: bool = False,
    ) -> None:
        _set_encrypted_json(
            NotificationConfig.KEY,
            {
                "smtp_host": smtp_host,
                "smtp_port": int(smtp_port or 587),
                "smtp_username": smtp_username,
                "smtp_password": smtp_password,
                "from_address": from_address,
                "use_starttls": bool(use_starttls),
                "use_ssl": bool(use_ssl),
            },
        )

    @staticmethod
    def load() -> dict[str, Any] | None:
        return _get_encrypted_json(NotificationConfig.KEY)


class MillenniumSession:
    """The Millennium Ultra web session AG Connect captured.

    Kept apart from PacsConfig because it has a different lifetime: the
    cookie expires and gets recaptured by the operator without any of the
    connection settings changing, and a reconnect must not be able to
    disturb them.
    """

    KEY = "millennium_session"

    @staticmethod
    def save(
        auth_cookie: str,
        base_url: str = "",
        company_name: str = "",
        time_offset: str = "",
        captured_at: str = "",
    ) -> None:
        _set_encrypted_json(
            MillenniumSession.KEY,
            {
                "auth_cookie": auth_cookie,
                "base_url": base_url,
                "company_name": company_name,
                # Millennium renders and parses its date fields against this
                # browser offset, so it travels with the cookie.
                "time_offset": time_offset,
                "captured_at": captured_at,
            },
        )

    @staticmethod
    def load() -> dict[str, Any] | None:
        return _get_encrypted_json(MillenniumSession.KEY)

    @staticmethod
    def clear() -> None:
        delete(MillenniumSession.KEY)

    @staticmethod
    def is_connected() -> bool:
        session = _get_encrypted_json(MillenniumSession.KEY) or {}
        return bool(session.get("auth_cookie"))


def is_configured() -> bool:
    """True when the wizard has produced a configuration the engine can run.

    A PACS that advertises `requires_connect` is not finished until its
    enrollment trigger has been chosen, which can only happen after the
    operator has signed in — so a saved-but-unconnected Millennium install
    reads as unconfigured and the engine stays parked.
    """
    if AccessGridConfig.load() is None:
        return False
    pacs = PacsConfig.load()
    if pacs is None:
        return False

    from .lib.pacs import get_descriptor

    descriptor = get_descriptor(pacs.get("vendor", ""))
    if descriptor is not None and descriptor.requires_connect:
        return bool((pacs.get("params") or {}).get("trigger_card_format"))
    return True
