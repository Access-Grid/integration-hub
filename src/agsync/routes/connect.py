"""AG Connect endpoints — hand-off of a captcha-gated PACS session.

The operator clicks a link, a browser opens, they sign in, and the wizard's
spinner turns into a card-format picker. Behind that:

  GET  /connect           page    → reconnect screen, once setup is done
  POST /connect/begin     wizard  → mint a launch id + one-shot AES key
  POST /connect/claim     sidecar → burn the launch id, collect the key
  POST /connect/callback  sidecar → deliver the sealed cookie
  POST /connect/cancel    sidecar → operator closed the window
  GET  /connect/status    wizard  → poll; renders spinner, error, or formats

Launches live in memory only, expire after LAUNCH_TTL_S, and are claimable
exactly once, so a stale link and a replayed callback both fail closed. The
key is minted per launch and discarded the moment the cookie is decrypted.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..ag import PROTOCOL_SEOS, template_protocol
from ..ag import build_client as build_ag_client
from ..auth import require_admin
from ..connect import register as uri_register
from ..connect.protocol import (
    MILLENNIUM_AUTH_COOKIE,
    b64url_encode,
    build_launch_uri,
    unseal,
)
from ..lib.pacs import build_adapter, get_descriptor
from ..lib.pacs.millennium_ultra.client import normalize_base_url
from ..notifications import reset_throttle
from ..settings_store import AccessGridConfig, MillenniumSession, PacsConfig
from ..sync import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connect")

# Long enough for a human to find the window, solve a captcha and sign in.
LAUNCH_TTL_S = 900


@dataclass
class Launch:
    launch_id: str
    key: bytes
    login_url: str
    created_at: float = field(default_factory=time.monotonic)
    claimed: bool = False
    state: str = "waiting"  # waiting | connected | cancelled | error
    message: str = ""

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created_at) > LAUNCH_TTL_S


class _LaunchRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._launches: dict[str, Launch] = {}

    def create(self, login_url: str) -> Launch:
        launch = Launch(
            launch_id=secrets.token_urlsafe(16),
            key=AESGCM.generate_key(bit_length=256),
            login_url=login_url,
        )
        with self._lock:
            self._prune()
            self._launches[launch.launch_id] = launch
        return launch

    def get(self, launch_id: str) -> Launch | None:
        with self._lock:
            launch = self._launches.get(launch_id)
            if launch and launch.expired:
                self._launches.pop(launch_id, None)
                return None
            return launch

    def waiting(self, login_url: str) -> Launch:
        """An unclaimed launch to hand out, reusing one where possible.

        The reconnect banner renders on every page, and minting a launch per
        page view would pile up ids nobody clicked. One live launch is all a
        person can act on at a time.
        """
        with self._lock:
            self._prune()
            for launch in self._launches.values():
                if not launch.claimed and launch.state == "waiting":
                    return launch
        return self.create(login_url)

    def claim(self, launch_id: str) -> Launch | None:
        """Hand out the key exactly once."""
        with self._lock:
            launch = self._launches.get(launch_id)
            if launch is None or launch.expired or launch.claimed:
                return None
            launch.claimed = True
            return launch

    def _prune(self) -> None:
        for key in [k for k, v in self._launches.items() if v.expired]:
            self._launches.pop(key, None)


_registry = _LaunchRegistry()


def _millennium_params() -> dict:
    cfg = PacsConfig.load() or {}
    return dict(cfg.get("params") or {})


def _login_url() -> str:
    """Where the side-car should point the browser.

    Prefer the URL saved with the connection settings; fall back to the one
    captured on a previous connect so a reconnect still works if the wizard
    is mid-flight.
    """
    params = _millennium_params()
    base = normalize_base_url(params.get("base_url") or "")
    if not base:
        base = normalize_base_url((MillenniumSession.load() or {}).get("base_url") or "")
    return f"{base}/Account/LogIn" if base else ""


def reconnect_link(request: Request) -> str:
    """Where the banner's "click here" should point.

    Straight at the side-car when it can be launched, so one click puts the
    login window on screen. Falls back to the connect page when the handler
    is not registered on this machine, since an agconnect:// link would
    otherwise do nothing at all.
    """
    login_url = _login_url()
    if not login_url or not uri_register.is_registered():
        return "/connect"
    launch = _registry.waiting(login_url)
    return build_launch_uri(launch.launch_id, _server_url(request))


def _server_url(request: Request) -> str:
    """The loopback address the side-car should call back on.

    Always loopback: the side-car runs on this machine, and the callback
    carries a live session.
    """
    return f"{request.url.scheme}://127.0.0.1:{request.url.port or 5355}"


# -- wizard-facing -----------------------------------------------------------


@router.post("/begin")
def connect_begin(request: Request, _user=Depends(require_admin)):
    login_url = _login_url()
    if not login_url:
        return JSONResponse(
            {"ok": False, "message": "Save the Millennium Ultra URL first."},
            status_code=400,
        )
    launch = _registry.create(login_url)
    logger.info("AG Connect: launch %s created for %s", launch.launch_id, login_url)
    return {
        "ok": True,
        "launch_id": launch.launch_id,
        "uri": build_launch_uri(launch.launch_id, _server_url(request)),
    }


@router.get("")
def connect_page(request: Request, _user=Depends(require_admin)):
    """Standalone reconnect screen, for when a live session has expired."""
    login_url = _login_url()
    launch_id = _registry.create(login_url).launch_id if login_url else ""
    return request.app.state.template_response(
        request, "connect.html",
        {
            "launch_id": launch_id,
            "no_url": not login_url,
            "uri_registered": uri_register.is_registered(),
            "engine_status": get_engine().get_status(),
        },
    )


@router.get("/status")
def connect_status(
    request: Request,
    launch: str = "",
    pick: int = 0,
    _user=Depends(require_admin),
):
    """HTMX poll target: spinner while waiting, then confirmation.

    During setup (`pick=1`) the confirmation is the card-format picker, since
    the trigger can only be read once a session exists. On a reconnect the
    trigger is already chosen, so it is just an acknowledgement.
    """
    record = _registry.get(launch) if launch else None
    connected = MillenniumSession.is_connected()
    fresh = record is not None and record.state == "connected"

    if connected and (record is None or fresh):
        if not pick:
            return request.app.state.template_response(
                request, "_connect_done.html", {"reconnected": fresh},
            )
        formats, error = _read_card_formats()
        return request.app.state.template_response(
            request,
            "wizard/_connect_formats.html",
            {
                "formats": formats,
                "error": error,
                "vendor": "millennium_ultra",
                "mode": _detected_mode(),
            },
        )

    state = record.state if record else ("expired" if launch else "waiting")
    message = record.message if record else ""
    return request.app.state.template_response(
        request,
        "wizard/_connect_waiting.html",
        {
            "launch": launch,
            "state": state,
            "message": message,
            "launch_uri": build_launch_uri(launch, _server_url(request)) if launch else "",
            "pick": pick,
        },
    )


def _detected_mode() -> str:
    """Which direction this install runs in, per the AccessGrid template."""
    ag_cfg = AccessGridConfig.load() or {}
    if not ag_cfg:
        return "desfire"
    try:
        client = build_ag_client(ag_cfg["account_id"], ag_cfg["api_secret"])
        protocol = template_protocol(client, ag_cfg["template_id"])
    except Exception as e:  # noqa: BLE001 — display only; never block setup
        logger.warning("Could not read the card template protocol: %s", e)
        return "desfire"
    return "seos" if protocol == PROTOCOL_SEOS else "desfire"


def _read_card_formats() -> tuple[list[dict], str]:
    """Read this install's card formats through the freshly-captured session."""
    descriptor = get_descriptor("millennium_ultra")
    if descriptor is None:
        return [], "Millennium Ultra adapter is not available"
    try:
        adapter = build_adapter("millennium_ultra", _millennium_params())
        return (
            [{"id": fid, "label": label} for fid, label in adapter.card_formats()],
            "",
        )
    except Exception as e:  # noqa: BLE001 — surfaced to the operator verbatim
        logger.warning("AG Connect: could not read card formats: %s", e)
        return [], f"{type(e).__name__}: {e}"


# -- side-car-facing ---------------------------------------------------------


@router.post("/claim")
async def connect_claim(request: Request):
    """Burn a launch and hand its one-shot key to the side-car.

    Unauthenticated on purpose: the side-car is a separate process with no
    session cookie. The launch id is the capability — unguessable, single
    use, and short-lived.
    """
    body = await request.json()
    launch = _registry.claim(str(body.get("launch_id", "")))
    if launch is None:
        return JSONResponse({"ok": False}, status_code=404)
    return {
        "ok": True,
        "key": b64url_encode(launch.key),
        "login_url": launch.login_url,
        "cookie_name": MILLENNIUM_AUTH_COOKIE,
    }


@router.post("/callback")
async def connect_callback(request: Request):
    body = await request.json()
    launch = _registry.get(str(body.get("launch_id", "")))
    if launch is None or not launch.claimed:
        return JSONResponse({"ok": False, "message": "Unknown launch"}, status_code=404)

    try:
        payload = unseal(launch.key, str(body.get("payload", "")))
    except Exception:  # noqa: BLE001 — never echo crypto detail outward
        logger.warning("AG Connect: could not decrypt the callback payload")
        launch.state = "error"
        launch.message = "The session could not be decrypted. Please try again."
        return JSONResponse({"ok": False}, status_code=400)

    cookies = {c["name"]: c["value"] for c in payload.get("cookies", [])}
    auth_cookie = cookies.get(MILLENNIUM_AUTH_COOKIE, "")
    if not auth_cookie:
        launch.state = "error"
        launch.message = "Sign-in did not produce a Millennium session cookie."
        return JSONResponse({"ok": False}, status_code=400)

    params = _millennium_params()
    base_url = normalize_base_url(params.get("base_url") or "")

    # Prove the session actually reads data before storing it. A cookie can
    # authenticate and still return nothing — a sign-in captured mid-flow
    # does exactly that — and the failure is silent downstream: an empty
    # roster looks like a PACS with no cardholders, which shows up as
    # "unreachable" long after the operator has walked away. Better to fail
    # here, where there is a human standing in front of a Try again button.
    ok, detail = _session_reads_cardholders(base_url, auth_cookie, cookies)
    if not ok:
        launch.state = "error"
        launch.message = (
            "That sign-in did not produce a working session. Please try "
            f"again and complete the login fully. ({detail})"
        )
        logger.warning("AG Connect: rejected an unusable session — %s", detail)
        return JSONResponse({"ok": False, "message": launch.message}, status_code=400)

    MillenniumSession.save(
        auth_cookie=auth_cookie,
        base_url=base_url,
        company_name=cookies.get("UltraCompanyName", ""),
        time_offset=cookies.get("timeoffset", ""),
        captured_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    # The key has done its job; drop it so the callback can't be replayed.
    launch.key = b""
    launch.state = "connected"
    # A fresh session clears the "reconnect" flag and re-arms the notifier so
    # the next expiry is reported promptly rather than swallowed by the
    # previous send window.
    reset_throttle("reconnect")
    # The adapter reads the session at construction, so the cached one is
    # still holding the dead cookie. The session was proven to read before
    # it was stored, so the reconnect banner can go now rather than
    # surviving until a cycle finishes.
    get_engine().session_restored()
    logger.info("AG Connect: session captured for launch %s", launch.launch_id)
    return {"ok": True}


def _session_reads_cardholders(
    base_url: str, auth_cookie: str, cookies: dict[str, str]
) -> tuple[bool, str]:
    """Can this session actually list cardholders? (ok, detail)."""
    from ..lib.pacs.millennium_ultra.client import MillenniumUltraClient

    client = None
    try:
        client = MillenniumUltraClient(
            base_url=base_url,
            auth_cookie=auth_cookie,
            company_name=cookies.get("UltraCompanyName", ""),
            time_offset=cookies.get("timeoffset", ""),
        )
        if not client.first_cardholder_id():
            return False, "signed in, but no cardholders were readable"
    except Exception as e:  # noqa: BLE001 — reported to the operator verbatim
        return False, f"{type(e).__name__}: {e}"
    finally:
        if client is not None:
            client.close()
    return True, ""


@router.post("/cancel")
async def connect_cancel(request: Request):
    body = await request.json()
    launch = _registry.get(str(body.get("launch_id", "")))
    if launch is not None:
        launch.state = "cancelled"
        launch.message = "The sign-in window was closed before finishing."
    return {"ok": True}
