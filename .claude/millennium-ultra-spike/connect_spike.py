"""Spike: `agsync connect` — capture a PACS web session from a controlled browser.

The CLI is a separate process from the running service, so the captured cookie
has to cross a process boundary. It travels through the browser as an encrypted
URL parameter: the service mints a per-launch AES-256-GCM key, the CLI encrypts
with it, and the browser carries only ciphertext to the callback.

Runs in the operator's own session (a plain CLI command), so there is no
Session 0 problem and no CreateProcessAsUser plumbing.

Flow:
  POST /connect/begin (get launch id + one-shot key)  ->  launch chromium --app
  ->  read DevToolsActivePort  ->  attach to the page target over CDP  ->  poll
  Network.getAllCookies until the auth cookie shows up  ->  AES-GCM seal it  ->
  Page.navigate to /connect/callback?id=..&d=<ciphertext>  ->  close, delete
  the temp profile.

Usage:
  python connect_spike.py <login_url> <cookie_name> <server_base_url>
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from websockets.sync.client import connect as ws_connect

POLL_INTERVAL_S = 1.0
LAUNCH_TIMEOUT_S = 20
LOGIN_TIMEOUT_S = 300

CHROME_CANDIDATES = [
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    # Windows
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> str:
    for path in CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    for name in ("google-chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("No Chromium-family browser found")


def launch(browser: str, url: str, profile: str) -> subprocess.Popen:
    args = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=520,760",
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_devtools_port(profile: str, deadline: float) -> int:
    portfile = os.path.join(profile, "DevToolsActivePort")
    while time.time() < deadline:
        if os.path.exists(portfile):
            head = open(portfile).read().splitlines()
            if head and head[0].strip().isdigit():
                return int(head[0].strip())
        time.sleep(0.2)
    raise TimeoutError("DevToolsActivePort never appeared — browser failed to start")


def page_target_ws(port: int, deadline: float) -> str:
    while time.time() < deadline:
        try:
            targets = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5).json()
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.3)
    raise TimeoutError("No page target exposed a websocket URL")


class CDP:
    def __init__(self, ws_url: str):
        self._ws = ws_connect(ws_url, max_size=32 * 1024 * 1024)
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        self._id += 1
        want = self._id
        self._ws.send(json.dumps({"id": want, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self._ws.recv(timeout=max(0.1, end - time.time())))
            # Skip event frames; we only care about our own reply.
            if msg.get("id") == want:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"{method} timed out")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def begin_launch(server: str) -> tuple[str, bytes]:
    """Ask the running service for a launch id and a one-shot encryption key."""
    r = httpx.post(f"{server}/connect/begin", timeout=10)
    r.raise_for_status()
    data = r.json()
    key = base64.urlsafe_b64decode(data["key"] + "=" * (-len(data["key"]) % 4))
    print(f"launch    : {data['launch_id']} (key {len(key) * 8}-bit, held by the service)")
    return data["launch_id"], key


def seal(key: bytes, cookies: list[dict]) -> str:
    """AES-GCM the cookie payload; nonce is prepended to the ciphertext."""
    nonce = os.urandom(12)
    plaintext = json.dumps({"cookies": [
        {"name": c["name"], "value": c["value"], "domain": c.get("domain", ""),
         "httpOnly": c.get("httpOnly", False)} for c in cookies
    ]}).encode()
    return b64url(nonce + AESGCM(key).encrypt(nonce, plaintext, None))


def capture(login_url: str, cookie_name: str, server: str, also: tuple[str, ...] = ()) -> dict | None:
    browser = find_browser()
    print(f"browser   : {browser}")
    profile = tempfile.mkdtemp(prefix="agsync-connect-")
    print(f"profile   : {profile}")

    launch_id, key = begin_launch(server)
    proc = launch(browser, login_url, profile)
    try:
        deadline = time.time() + LAUNCH_TIMEOUT_S
        port = wait_for_devtools_port(profile, deadline)
        print(f"cdp port  : {port}")

        ws_url = page_target_ws(port, deadline)
        cdp = CDP(ws_url)
        cdp.call("Network.enable")
        print(f"waiting   : for cookie {cookie_name!r} (log in, then it fires)\n")

        found = None
        end = time.time() + LOGIN_TIMEOUT_S
        while time.time() < end:
            if proc.poll() is not None:
                print("window closed by the operator — cancelled")
                return None
            cookies = cdp.call("Network.getAllCookies").get("cookies", [])
            for c in cookies:
                if c.get("name") == cookie_name and c.get("value"):
                    found = c
                    break
            if found:
                break
            time.sleep(POLL_INTERVAL_S)

        if not found:
            print("timed out waiting for the cookie")
            return None

        wanted = (cookie_name,) + also
        payload_cookies = [c for c in cookies if c["name"] in wanted and c.get("value")]
        names = sorted({c["name"] for c in cookies})
        print(f"CAPTURED  : {found['name']} ({len(found['value'])} chars, httpOnly={found.get('httpOnly')})")
        print(f"  domain  : {found.get('domain')}")
        print(f"  jar     : {len(cookies)} cookies -> {', '.join(names[:10])}")
        print(f"  sending : {[c['name'] for c in payload_cookies]}")

        sealed = seal(key, payload_cookies)
        callback = f"{server}/connect/callback?id={launch_id}&d={sealed}"
        print(f"sealed    : {len(sealed)} chars of ciphertext in the URL")
        print(f"redirect  : {callback[:96]}...")
        cdp.call("Page.navigate", {"url": callback})
        time.sleep(3)

        cdp.close()
        return found
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
        print("cleaned up : browser closed, profile deleted")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    url, name = sys.argv[1], sys.argv[2]
    server = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8921"
    result = capture(url, name, server, also=("UltraCompanyName",))
    raise SystemExit(0 if result else 1)
