"""The contract between the agsync service and the AG Connect side-car.

Some PACS logins are behind a captcha, so no amount of scripting can
authenticate for the operator — a human has to type it. AG Connect is the
smallest thing that makes that human's session usable by a background
service: it opens a throwaway Chromium at the PACS login page, waits for the
operator to sign in, lifts the resulting session cookie, and hands it back.

Which cookie that is, and what else travels with it, is the vendor's
business and reaches the side-car over the wire — see `BrowserLogin` on the
PACS descriptor. Nothing in this package names a PACS.

Two process boundaries have to be crossed, and each is deliberately narrow:

  service → side-car   The service can't spawn the browser itself. It runs
    as a Windows service in session 0, where a launched window would be
    invisible on the operator's desktop, so the *browser* starts the
    conversation: the page offers an ``agconnect://`` link, the OS hands it
    to the registered side-car, and the side-car calls back in. The URI
    carries only a launch id and where to phone home — never a key.

  side-car → service   The captured cookie is a live credential, so it is
    sealed with AES-256-GCM under a one-shot key that never leaves the
    service except to the side-car that claimed this launch. Claiming burns
    the launch, so a replayed URI gets nothing and a replayed callback
    cannot be decrypted.
"""

from __future__ import annotations

import base64
import json
import os
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

URI_SCHEME = "agconnect"
URI_VERSION = "v1"


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def build_launch_uri(launch_id: str, server_url: str) -> str:
    """The link the wizard renders for the operator to click."""
    query = urlencode({"launch": launch_id, "server": server_url})
    return f"{URI_SCHEME}://{URI_VERSION}/connect?{query}"


def parse_launch_uri(uri: str) -> tuple[str, str]:
    """(launch_id, server_url) from an agconnect:// URI."""
    parsed = urlparse(uri)
    if parsed.scheme != URI_SCHEME:
        raise ValueError(f"Not an {URI_SCHEME}:// URI: {uri!r}")
    params = parse_qs(parsed.query)
    launch_id = (params.get("launch") or [""])[0]
    server_url = (params.get("server") or [""])[0]
    if not launch_id or not server_url:
        raise ValueError("Launch URI is missing 'launch' or 'server'")
    return launch_id, server_url


def seal(key: bytes, payload: dict) -> str:
    """AES-GCM the payload; the nonce is prepended to the ciphertext."""
    nonce = os.urandom(12)
    plaintext = json.dumps(payload).encode()
    return b64url_encode(nonce + AESGCM(key).encrypt(nonce, plaintext, None))


def unseal(key: bytes, sealed: str) -> dict:
    blob = b64url_decode(sealed)
    nonce, ciphertext = blob[:12], blob[12:]
    return json.loads(AESGCM(key).decrypt(nonce, ciphertext, None))
