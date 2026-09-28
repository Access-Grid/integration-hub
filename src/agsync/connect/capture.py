"""Drive a throwaway Chromium and lift the PACS session cookie.

The operator sees a small browser window at the PACS login page, solves the
captcha, and signs in as themselves. We watch the cookie jar over the
DevTools protocol and, the moment the forms-auth cookie appears, seal it and
hand it to the agsync service.

Everything about the browser is disposable: a fresh `--user-data-dir` under
the system temp directory, deleted on the way out, so nothing of the
operator's own profile is touched or left behind. The debugging port is
bound on an ephemeral port and only ever reached over loopback.

If the operator closes the window, we notice the process exit and report a
cancellation rather than hanging until the timeout.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time

import httpx
from websockets.sync.client import connect as ws_connect

from .protocol import seal

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 1.0
LAUNCH_TIMEOUT_S = 30
LOGIN_TIMEOUT_S = 600
# The auth cookie is set on the login POST's redirect, before the browser has
# followed it, and the app can re-issue it as the landing page loads. Taking
# the first value seen can therefore capture a session that authenticates but
# reads nothing, so let the login settle and use whatever it ends on.
SETTLE_S = 4.0

BROWSER_CANDIDATES = (
    # Windows — the operator's machine in practice.
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


class ConnectError(RuntimeError):
    pass


def find_browser() -> str:
    for path in BROWSER_CANDIDATES:
        if os.path.exists(path):
            return path
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    raise ConnectError(
        "No Chromium-family browser found. Install Google Chrome or Microsoft Edge."
    )


class _CDP:
    """Just enough DevTools protocol to read the cookie jar."""

    def __init__(self, ws_url: str):
        self._ws = ws_connect(ws_url, max_size=32 * 1024 * 1024)
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        self._id += 1
        want = self._id
        self._ws.send(json.dumps({"id": want, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = json.loads(self._ws.recv(timeout=max(0.1, deadline - time.time())))
            # Event frames share the socket; only our own reply matters.
            if message.get("id") == want:
                if "error" in message:
                    raise ConnectError(f"{method}: {message['error']}")
                return message.get("result", {})
        raise ConnectError(f"{method} timed out")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 — best effort on the way out
            pass


def _launch(browser: str, url: str, profile_dir: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            browser,
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=560,800",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_devtools_port(profile_dir: str, deadline: float) -> int:
    port_file = os.path.join(profile_dir, "DevToolsActivePort")
    while time.time() < deadline:
        if os.path.exists(port_file):
            head = open(port_file).read().splitlines()
            if head and head[0].strip().isdigit():
                return int(head[0].strip())
        time.sleep(0.2)
    raise ConnectError("Browser failed to start (no DevTools port)")


def _page_socket(port: int, deadline: float) -> str:
    while time.time() < deadline:
        try:
            targets = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5).json()
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
        except Exception:  # noqa: BLE001 — the browser may not be listening yet
            pass
        time.sleep(0.3)
    raise ConnectError("Browser exposed no debuggable page")


def capture_session(
    login_url: str,
    key: bytes,
    cookie_name: str,
    also: tuple[str, ...] = (),
    on_status=None,
    pacs_name: str = "the PACS",
) -> str | None:
    """Run the login window; return the sealed payload, or None if cancelled.

    `cookie_name` is the one whose appearance means the human got through;
    `also` are companions the PACS screens expect alongside it. Both come
    from the vendor's descriptor, so this function never learns whose login
    it just watched — `pacs_name` is for the operator's benefit only.
    """
    def status(message: str) -> None:
        logger.info("AG Connect: %s", message)
        if on_status:
            on_status(message)

    browser = find_browser()
    status(f"launching {os.path.basename(browser)}")
    profile_dir = tempfile.mkdtemp(prefix="agsync-connect-")
    process = _launch(browser, login_url, profile_dir)
    cdp: _CDP | None = None
    try:
        deadline = time.time() + LAUNCH_TIMEOUT_S
        port = _wait_for_devtools_port(profile_dir, deadline)
        cdp = _CDP(_page_socket(port, deadline))
        cdp.call("Network.enable")
        status(f"waiting for you to sign in to {pacs_name}")

        jar: list[dict] = []
        found: dict | None = None
        login_deadline = time.time() + LOGIN_TIMEOUT_S
        while time.time() < login_deadline:
            if process.poll() is not None:
                status("window closed before sign-in finished")
                return None
            jar = cdp.call("Network.getAllCookies").get("cookies", [])
            found = next(
                (c for c in jar if c.get("name") == cookie_name and c.get("value")),
                None,
            )
            if found:
                break
            time.sleep(POLL_INTERVAL_S)

        if not found:
            status("timed out waiting for sign-in")
            return None

        time.sleep(SETTLE_S)
        settled = cdp.call("Network.getAllCookies").get("cookies", [])
        final = next(
            (c for c in settled if c.get("name") == cookie_name and c.get("value")),
            None,
        )
        if final is not None:
            if final["value"] != found["value"]:
                logger.info("AG Connect: session was re-issued while settling")
            jar, found = settled, final

        wanted = (cookie_name, *also)
        cookies = [c for c in jar if c["name"] in wanted and c.get("value")]
        status(f"captured {len(cookies)} cookie(s) — handing the session back")
        return seal(key, {
            "cookies": [
                {"name": c["name"], "value": c["value"], "domain": c.get("domain", "")}
                for c in cookies
            ]
        })
    finally:
        if cdp is not None:
            cdp.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            process.kill()
        shutil.rmtree(profile_dir, ignore_errors=True)
        logger.info("AG Connect: browser closed, temporary profile deleted")
