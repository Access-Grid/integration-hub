"""Signing and sending a status report.

Deliberately not routed through the `accessgrid` SDK. Its `_make_request`
calls `requests.request()` with no timeout, and `requests` has no default
one, so a hung connection blocks the calling thread for as long as the
socket stays open. A reporting thread parked on that call would stop
reporting without ever raising — the one failure a heartbeat must not have.

So the HMAC scheme is implemented here against httpx, which is already a
pinned dependency, with an explicit timeout. The scheme is AccessGrid's
standard one: hex HMAC-SHA256 over Base64 of the exact request body, keyed
with the account secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Provisional: the AccessGrid side has not confirmed this path yet, which is
# why reporting is off by default. One constant to change when they do.
STATUS_PATH = "/v1/console/integration-hub/status"

# Kept comfortably under the shortest reporting interval so a slow endpoint
# can never cause beats to queue up behind each other.
TIMEOUT = httpx.Timeout(5.0, connect=3.0)


class StatusRejected(Exception):
    """The endpoint answered, and said no."""

    def __init__(self, status_code: int, retry_after: float | None = None):
        super().__init__(f"status endpoint returned {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


def sign(body: str, secret: str) -> str:
    """AccessGrid's payload signature: HMAC-SHA256 over Base64(body)."""
    return hmac.new(
        secret.encode("utf-8"),
        base64.b64encode(body.encode("utf-8")),
        hashlib.sha256,
    ).hexdigest()


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        # The header also allows an HTTP date, which we do not need badly
        # enough to parse.
        return None


def send(
    report: dict[str, Any],
    *,
    account_id: str,
    secret: str,
    base_url: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """POST one report. Returns the decoded body, or {} when there is none.

    Raises `StatusRejected` for a non-2xx answer and `httpx.HTTPError` for a
    transport failure. Both are the caller's to swallow.
    """
    # Signed over the exact bytes sent: re-serialising with different
    # separators would invalidate the signature.
    body = json.dumps(report, separators=(",", ":"), sort_keys=True)
    headers = {
        "X-ACCT-ID": account_id,
        "X-PAYLOAD-SIG": sign(body, secret),
        "Content-Type": "application/json",
    }
    url = base_url.rstrip("/") + STATUS_PATH

    owned = client is None
    http = client or httpx.Client(timeout=TIMEOUT)
    try:
        response = http.post(url, content=body.encode("utf-8"), headers=headers)
    finally:
        if owned:
            http.close()

    if not 200 <= response.status_code < 300:
        raise StatusRejected(response.status_code, _retry_after(response))

    if response.status_code == 204 or not response.content:
        return {}
    try:
        decoded = response.json()
    except ValueError:
        logger.debug("Status endpoint returned a non-JSON body; ignoring it")
        return {}
    return decoded if isinstance(decoded, dict) else {}
