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
from datetime import datetime
from email.message import EmailMessage

from .settings_store import NotificationConfig, PacsConfig, get_json, set_json

logger = logging.getLogger(__name__)

# One reconnect nag per this many seconds, however often the engine retries.
RESEND_INTERVAL_S = 6 * 60 * 60

_lock = threading.Lock()
_last_sent: dict[str, float] = {}

# Avigilon Unity sends at most this many emails in any 24 hours, whatever
# happens. The per-problem throttle above does not bound the total: each card
# with a problem has its own key, and a restart forgets every key. This does,
# because every email goes through send_mail and the send times are stored
# in the database, so a restart remembers them. Each attempt counts, even
# one the relay then refuses: a relay that errors after sending would
# otherwise be retried past the limit.
MAX_MAILS_PER_DAY = 6
_DAY_S = 24 * 60 * 60
_SENT_AT_KEY = "mail_sent_at"
_CAPPED_VENDORS = frozenset({"avigilon"})
_cap_lock = threading.Lock()


def _capped() -> bool:
    """Does the daily limit apply? An unreadable setup counts as yes."""
    try:
        return (PacsConfig.load() or {}).get("vendor") in _CAPPED_VENDORS
    except Exception:  # noqa: BLE001
        return True


def _sent_today(now: float) -> list[float]:
    """Send times within the last 24 hours. A time more than a day ahead
    (a clock set back) is dropped, so it cannot block mail for good."""
    stored = get_json(_SENT_AT_KEY) or []
    return sorted(t for t in stored if isinstance(t, int | float) and not isinstance(t, bool)
                  and now - _DAY_S < t <= now + _DAY_S)


def _limit_reached(subject: str) -> bool:
    """Has today's limit been reached? Checks only, and logs if so.

    Checked before a per-problem throttle is used up, so a problem held back
    by the limit is mailed once mail resumes, not 6 hours after that.
    """
    if not _capped():
        return False
    now = time.time()
    try:
        with _cap_lock:
            sent = _sent_today(now)
    except Exception as e:  # noqa: BLE001
        logger.error("Mail limit: the record of emails sent could not be read (%s) — not emailing %r, so "
                     "the limit of %d a day cannot be passed", e, subject, MAX_MAILS_PER_DAY)
        return True
    if len(sent) < MAX_MAILS_PER_DAY:
        return False
    _log_limit(sent, subject)
    return True


def _log_limit(sent: list[float], subject: str) -> None:
    resumes = datetime.fromtimestamp(sent[0] + _DAY_S).isoformat(sep=" ", timespec="minutes")
    logger.error("Mail limit: %d emails sent in the last 24 hours, the most this sync sends — not emailing %r. "
                 "The problem is in the log just above. Mail resumes at %s.", len(sent), subject, resumes)


def _take_slot(subject: str) -> tuple[bool, int]:
    """(may this email go, how many this makes in the last 24 hours).

    Records the send before it happens, so two threads cannot both take the
    last slot. A record that cannot be read or written sends nothing.
    """
    if not _capped():
        return True, 0
    now = time.time()
    with _cap_lock:
        try:
            sent = _sent_today(now)
            if len(sent) >= MAX_MAILS_PER_DAY:
                _log_limit(sent, subject)
                return False, len(sent)
            sent.append(now)
            set_json(_SENT_AT_KEY, sent)
        except Exception as e:  # noqa: BLE001
            logger.error("Mail limit: the record of emails sent could not be read or written (%s) — not "
                         "emailing %r, so the limit of %d a day cannot be passed", e, subject, MAX_MAILS_PER_DAY)
            return False, 0
    return True, len(sent)


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

    if recipient and _limit_reached(subject):
        return False
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


def notify_card_problem(key: str, problem: str) -> bool:
    """Mail the operator about a card the sync will not act on. True if mailed.

    The caller logs the problem where it finds it; this only sends it on,
    once per `key` (one card) per RESEND_INTERVAL_S.
    """
    recipient = ((PacsConfig.load() or {}).get("params") or {}).get("notify_email", "")
    subject = "AccessGrid Sync: a card needs attention"
    if not recipient or _limit_reached(subject) or not _should_send(f"card:{key}"):
        return False
    return send_mail(
        recipient,
        subject,
        f"{problem}\n\nThe pass is left as it is until this is resolved in the PACS.\n",
    )


def send_mail(recipient: str, subject: str, body: str) -> bool:
    config = NotificationConfig.load() or {}
    host = (config.get("smtp_host") or "").strip()
    if not host:
        logger.warning(
            "SMTP is not configured — '%s' was logged but not emailed to %s",
            subject, recipient,
        )
        return False

    allowed, count = _take_slot(subject)
    if not allowed:
        return False
    if count == MAX_MAILS_PER_DAY:
        body += (f"\nThis is email {count} of {MAX_MAILS_PER_DAY}, the most AccessGrid Sync sends in 24 hours. "
                 "Until a day has passed since the first of them, further problems are written to the "
                 "sync log only.\n")

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
