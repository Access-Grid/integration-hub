"""The thread that reports this hub's status to AccessGrid.

Everything here is arranged around one rule: **reporting must never be able
to disturb the thing it reports on.** A heartbeat that breaks a sync is
worse than no heartbeat at all. Concretely:

* It is its own daemon thread, not a step in the sync loop. Cycles are
  floored at a minute and can run for several, so a beat riding on them
  would fall silent exactly when something is wrong — which is when the
  report matters most.
* A beat reads memory and makes one HTTP call. Nothing in the tick path
  touches the database: the cycle counts come from a snapshot the engine
  leaves behind, and the account configuration is cached, so the reporter
  never contends with the sync thread for SQLite.
* No lock is held across the network. `get_status()` copies under the
  engine's lock and returns; the payload is built and sent afterwards.
* The HTTP timeout is well under the interval, so beats cannot pile up, and
  one beat is in flight at a time by construction.
* The loop body cannot raise. Every tick is wrapped, failures are logged at
  debug and the thread stays up. It cannot die and it cannot propagate.
* Failures back off, so a dead or wrong endpoint is not hammered every
  fifteen seconds for the lifetime of the install.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from . import payload, transport
from .fingerprint import describe

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 15
# AccessGrid sends no cadence today — 204, no body. The clamp is kept
# anyway, so that if one ever appears, a mistaken or hostile value can
# neither switch reporting off nor turn this into a hot loop.
MIN_INTERVAL_S = 15
MAX_INTERVAL_S = 300

# Consecutive-failure backoff, then a hold at the last value.
BACKOFF_S = (15, 30, 60, 120, 300)
# A rejected or missing endpoint will not start working because we asked
# again quickly.
HARD_BACKOFF_S = 300
HARD_FAILURES = frozenset({401, 403, 404})

# The account configuration lives in the encrypted settings table, so it is
# read rarely rather than on every beat.
CONFIG_TTL_S = 60.0


class StatusReporter:
    """Posts a status report on a fixed cadence until stopped."""

    def __init__(
        self,
        engine: Any,
        *,
        interval_s: int = DEFAULT_INTERVAL_S,
        base_url: str = transport.PRODUCTION_BASE_URL,
    ) -> None:
        self._engine = engine
        self._interval_s = float(max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, interval_s)))
        self._base_url = base_url
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started_at = time.time()
        self._failures = 0
        self._cfg: dict[str, Any] | None = None
        self._cfg_read_at = 0.0
        # One client, so beats reuse a warm connection instead of paying for
        # a TLS handshake every fifteen seconds.
        self._client: httpx.Client | None = None

    # ----- lifecycle --------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="status-reporter"
        )
        self._thread.start()
        logger.info("Status reporting every %ds", int(self._interval_s))

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._close_client()

    def _close_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception as e:  # noqa: BLE001 — shutdown must not raise
                logger.debug("Closing the status client failed: %s", e)
            self._client = None

    # ----- the loop ---------------------------------------------------

    def _run_loop(self) -> None:
        # The outer guard is belt and braces: `_beat` already swallows
        # everything, so reaching the except means a bug in the loop itself,
        # and even then the thread should go quietly rather than print a
        # traceback into an operator's console.
        try:
            while not self._stop.is_set():
                wait_s = self._beat()
                self._stop.wait(timeout=wait_s)
        except Exception as e:  # noqa: BLE001
            logger.debug("Status reporting stopped: %s", e)

    def _beat(self) -> float:
        """Send one report. Returns how long to wait before the next.

        Never raises.
        """
        try:
            cfg = self._config()
            if cfg is None:
                # Not set up yet. Nothing to report to and nobody to tell.
                return self._interval_s

            report = self._report(cfg)
            response = transport.send(
                report,
                account_id=cfg["account_id"],
                secret=cfg["api_secret"],
                base_url=self._base_url,
                client=self._http_client(),
            )
            self._failures = 0
            return self._interval_from(response)

        except transport.StatusRejected as e:
            self._failures += 1
            if e.retry_after is not None:
                return max(MIN_INTERVAL_S, min(900.0, e.retry_after))
            if e.status_code in HARD_FAILURES:
                logger.debug(
                    "Status endpoint rejected the report (%d) — backing off",
                    e.status_code,
                )
                return float(HARD_BACKOFF_S)
            return self._backoff()
        except httpx.HTTPError as e:
            self._failures += 1
            logger.debug("Could not reach the status endpoint: %s", e)
            return self._backoff()
        except Exception as e:  # noqa: BLE001 — a beat must never escape
            self._failures += 1
            logger.debug("Status report failed to build or send: %s", e)
            return self._backoff()

    def _backoff(self) -> float:
        index = min(self._failures, len(BACKOFF_S)) - 1
        return float(BACKOFF_S[max(index, 0)])

    def _interval_from(self, response: dict[str, Any]) -> float:
        """Honour a server-advised cadence, within our own bounds."""
        advised = response.get("report_interval_s")
        if advised is None:
            return self._interval_s
        try:
            wanted = float(advised)
        except (TypeError, ValueError):
            return self._interval_s
        return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, wanted))

    # ----- the report -------------------------------------------------

    def _http_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=transport.TIMEOUT)
        return self._client

    def _config(self) -> dict[str, Any] | None:
        """The AccessGrid credentials, re-read at most once a minute."""
        now = time.time()
        # Gated on when it was read, not on what was found. Keying off the
        # value meant an unconfigured hub — where the answer is None — went
        # back to the database on every single beat, which is exactly the
        # contention this cache exists to avoid, in the likeliest case.
        if self._cfg_read_at and now - self._cfg_read_at < CONFIG_TTL_S:
            return self._cfg
        from ..settings_store import AccessGridConfig

        cfg = AccessGridConfig.load()
        self._cfg_read_at = now
        self._cfg = cfg if cfg and cfg.get("account_id") and cfg.get("api_secret") else None
        return self._cfg

    def _report(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """Build the body from one copied snapshot of engine state.

        `get_status()` takes the engine's lock, copies, and returns — so
        nothing below this line holds a lock or touches the database.
        """
        status = self._engine.get_status()
        return payload.build(
            status,
            instance=describe(),
            uptime_s=int(time.time() - self._started_at),
            vendor=status.get("pacs_vendor") or "",
            display_name=status.get("pacs_display_name") or "",
            writes_credentials=status.get("pacs_writes_credentials"),
            totals=status.get("totals") or {},
        )
