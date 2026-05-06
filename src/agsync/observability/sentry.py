"""Sentry initialization.

DSN is baked into the binary at build time (set the
`AG_SYNC_SENTRY_DSN` env var when running PyInstaller). At runtime, an
empty DSN disables Sentry entirely.

`user.id` is the AccessGrid account_id once the wizard runs, so events
are grouped by customer.
"""

from __future__ import annotations

import logging

import sentry_sdk

from .. import __version__
from ..config import get_settings
from ..settings_store import AccessGridConfig

logger = logging.getLogger(__name__)


def init_sentry() -> None:
    dsn = get_settings().sentry_dsn
    if not dsn:
        logger.info("Sentry: DSN not configured — telemetry disabled")
        return

    sentry_sdk.init(
        dsn=dsn,
        release=f"ag-sync-tool@{__version__}",
        traces_sample_rate=0.0,
        send_default_pii=False,
        max_breadcrumbs=50,
        before_send=_attach_user,
    )
    logger.info("Sentry: initialized (release=ag-sync-tool@%s)", __version__)


def _attach_user(event, _hint):  # type: ignore[no-untyped-def]
    try:
        ag = AccessGridConfig.load()
        if ag and ag.get("account_id"):
            event.setdefault("user", {})
            event["user"]["id"] = ag["account_id"]
    except Exception:  # noqa: BLE001
        pass
    return event
