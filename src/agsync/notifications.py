"""Operator notifications — currently one message, and an important one.

Millennium Ultra's session cannot be renewed without a human: the login is
captcha-gated, so when the captured cookie expires the sync engine simply
stops until someone signs in again. Nobody is watching a NUC's web UI, so we
send mail to the address collected during setup.

SMTP details are optional. When they are absent the message is still logged
at ERROR and the reconnect banner still appears in the UI — the integration
degrades to "visible in the app" rather than failing silently or refusing to
run. Repeat sends are throttled so a stuck session doesn't mail every cycle.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage

from .settings_store import NotificationConfig, PacsConfig

logger = logging.getLogger(__name__)

# One reconnect nag per this many seconds, however often the engine retries.
RESEND_INTERVAL_S = 6 * 60 * 60

_lock = threading.Lock()
_last_sent: dict[str, float] = {}


def _should_send(key: str) -> bool:
    now = time.monotonic()
    with _lock:
        last = _last_sent.get(key)
        if last is not None and (now - last) < RESEND_INTERVAL_S:
            return False
        _last_sent[key] = now
        return True


def reset_throttle(key: str = "") -> None:
    """Forget the send history so the next event mails immediately.

    Called once a session is recaptured, so a later expiry is reported
    promptly rather than swallowed by the previous window.
    """
    with _lock:
        if key:
            _last_sent.pop(key, None)
        else:
            _last_sent.clear()


def notify_reconnect_required(vendor_name: str, detail: str = "") -> bool:
    """Tell the operator their PACS session needs a human. True if mailed."""
    recipient = ((PacsConfig.load() or {}).get("params") or {}).get("notify_email", "")
    subject = f"AccessGrid Sync: sign in to {vendor_name} again"
    body = (
        f"The {vendor_name} session AccessGrid Sync was using has expired, and "
        "syncing is paused until it is replaced.\n\n"
        "Open the AccessGrid Sync web UI on the sync machine and use "
        "\"Reconnect\" to sign in again. Because the login is protected by a "
        "captcha, this cannot be done automatically.\n"
    )
    if detail:
        body += f"\nDetail: {detail}\n"

    if not _should_send("reconnect"):
        logger.debug("Reconnect notice already sent recently — not resending")
        return False

    logger.error(
        "%s session expired — reconnect required%s",
        vendor_name, f" ({recipient})" if recipient else "",
    )
    if not recipient:
        logger.warning("No notification address configured — not sending mail")
        return False
    return send_mail(recipient, subject, body)


def send_mail(recipient: str, subject: str, body: str) -> bool:
    config = NotificationConfig.load() or {}
    host = (config.get("smtp_host") or "").strip()
    if not host:
        logger.warning(
            "SMTP is not configured — '%s' was logged but not emailed to %s",
            subject, recipient,
        )
        return False

    message = EmailMessage()
    message["From"] = config.get("from_address") or f"agsync@{host}"
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    port = int(config.get("smtp_port") or 587)
    username = config.get("smtp_username") or ""
    password = config.get("smtp_password") or ""
    use_ssl = bool(config.get("use_ssl"))
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        with server:
            if not use_ssl and bool(config.get("use_starttls", True)):
                server.starttls(context=ssl.create_default_context())
            if username:
                server.login(username, password)
            server.send_message(message)
    except Exception as e:  # noqa: BLE001 — mail must never break a sync cycle
        logger.error("Failed to send notification to %s: %s", recipient, e)
        return False
    logger.info("Notification sent to %s: %s", recipient, subject)
    return True
