"""Spike: stands in for the running agsync service.

Two endpoints, mirroring what routes/settings.py would grow:

  POST /connect/begin     -> mints {launch_id, key} and remembers it in memory
  GET  /connect/callback  -> ?id=<launch_id>&d=<b64url AES-GCM ciphertext>
                             decrypts, verifies, "stores" the session,
                             and renders the success page the operator sees.

The key never leaves this process except to the connect CLI, and the browser
only ever carries ciphertext.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import secrets
import urllib.parse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PORT = 8921
LAUNCHES: dict[str, bytes] = {}     # launch_id -> key
STORED: dict[str, str] = {}         # what the settings blob would hold


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "text/html") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/connect/begin":
            return self._send(404, b"no")
        launch_id = secrets.token_urlsafe(12)
        key = AESGCM.generate_key(bit_length=256)
        LAUNCHES[launch_id] = key
        print(f"[server] /connect/begin -> launch {launch_id}, minted a 256-bit key")
        self._send(200, json.dumps({
            "launch_id": launch_id,
            "key": base64.urlsafe_b64encode(key).decode().rstrip("="),
        }).encode(), "application/json")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/connect/callback":
            return self._send(404, b"no")
        q = urllib.parse.parse_qs(parsed.query)
        launch_id = (q.get("id") or [""])[0]
        payload = (q.get("d") or [""])[0]

        print(f"[server] callback  id={launch_id} ciphertext={len(payload)} chars")
        print(f"[server] raw query : {parsed.query[:110]}...")

        key = LAUNCHES.pop(launch_id, None)
        if key is None:
            print("[server] REJECTED: unknown or already-used launch id")
            return self._send(400, b"<h1>Rejected</h1><p>Unknown launch.</p>")

        try:
            blob = b64url_decode(payload)
            nonce, ct = blob[:12], blob[12:]
            data = json.loads(AESGCM(key).decrypt(nonce, ct, None))
        except Exception as e:
            print(f"[server] REJECTED: decrypt failed ({type(e).__name__})")
            return self._send(400, b"<h1>Rejected</h1><p>Bad payload.</p>")

        for c in data["cookies"]:
            STORED[c["name"]] = c["value"]
        print(f"[server] DECRYPTED and stored {len(data['cookies'])} cookie(s):")
        for c in data["cookies"]:
            print(f"[server]   {c['name']} = {c['value'][:32]}... ({len(c['value'])} chars, httpOnly={c['httpOnly']})")
        print(f"[server] key for {launch_id} discarded — a replay of this URL now fails")

        self._send(200, b"<h1>Connected</h1><p>You can close this window.</p>")

    def log_message(self, *a):  # quiet
        pass


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    print(f"[server] listening on 127.0.0.1:{PORT}")
    http.server.HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
