"""The `agsync connect` side-car: claim a launch, capture, hand back.

This is what the OS starts when the operator clicks the wizard's
``agconnect://`` link. It runs in the operator's own desktop session, which
is the entire point — the service itself lives in session 0 and could not
put a browser window on their screen.

The service is reached over loopback with a self-signed certificate, so
verification is disabled for these three calls; that is safe here because
the cookie is already sealed under a key only that service holds, and a
launch can be claimed exactly once.
"""

from __future__ import annotations

import logging

import httpx

from .capture import ConnectError, capture_session
from .protocol import b64url_decode, parse_launch_uri

logger = logging.getLogger(__name__)

CLAIM_TIMEOUT_S = 20.0


def _client(server_url: str) -> httpx.Client:
    return httpx.Client(base_url=server_url, timeout=CLAIM_TIMEOUT_S, verify=False)


def run_from_uri(uri: str, on_status=None) -> int:
    """Handle one agconnect:// launch. Returns a process exit code."""
    def status(message: str) -> None:
        logger.info("%s", message)
        if on_status:
            on_status(message)

    try:
        launch_id, server_url = parse_launch_uri(uri)
    except ValueError as e:
        status(f"Bad launch link: {e}")
        return 2

    try:
        with _client(server_url) as http:
            claim = http.post("/connect/claim", json={"launch_id": launch_id})
            if claim.status_code != 200:
                status(
                    "This sign-in link is no longer valid — start again from "
                    "the AccessGrid Sync setup page."
                )
                return 3
            details = claim.json()
            key = b64url_decode(details["key"])

            sealed = capture_session(
                details["login_url"],
                key,
                cookie_name=details.get("cookie_name") or ".AspNet.UltraAuth",
                on_status=on_status,
            )
            if sealed is None:
                http.post("/connect/cancel", json={"launch_id": launch_id})
                return 1

            done = http.post(
                "/connect/callback", json={"launch_id": launch_id, "payload": sealed},
            )
            if done.status_code != 200:
                status(f"AccessGrid Sync rejected the session ({done.status_code})")
                return 4
    except ConnectError as e:
        status(str(e))
        return 5
    except httpx.HTTPError as e:
        status(f"Could not reach AccessGrid Sync at {server_url}: {e}")
        return 6

    status("Connected. You can return to the AccessGrid Sync setup page.")
    return 0
