"""Sync engine — main loop coordinating the six phases.

Single global instance held in this module. Started at app boot and
stopped at shutdown. Manual sync from the web UI calls
`engine.trigger_now()` which sets a flag the loop notices on its next
wake.

Lifecycle:
  - thread is created in start() and stays alive until stop()
  - dynamic interval: 3 × snapshot build time, clamped [10s, 600s]
  - circuit breaker: 10 consecutive failures pause the engine
  - status payload exposed via get_status() for the web UI

One failure is special. A PACS whose session was handed to us by a human
(Millennium Ultra, whose login is captcha-gated) can lose that session, and
no retry will ever fix it. PacsAuthExpired therefore short-circuits the
cycle: the engine flags `reconnect_required`, mails the operator, and stops
touching AccessGrid — an expired session must never be mistaken for a PACS
that suddenly has no cardholders left.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from ..ag import PROTOCOL_SEOS, template_protocol
from ..ag import build_client as build_ag_client
from ..lib.pacs import PacsAuthExpired, get_descriptor
from ..lib.pacs import build_adapter as build_pacs_adapter
from ..settings_store import AccessGridConfig, PacsConfig
from .phases import (
    phase1_provision,
    phase2_local_to_ag,
    phase3_deletions,
    phase4_ag_to_local,
    phase5_retries,
    phase6_field_changes,
)
from .snapshot import build_snapshot

logger = logging.getLogger(__name__)

MIN_INTERVAL_S = 10
MAX_INTERVAL_S = 600
INTERVAL_MULTIPLIER = 3
ERROR_BACKOFF_S = 30
MAX_CONSECUTIVE_ERRORS = 10


def derived_pacs_params(ag, ag_cfg: dict[str, Any], pacs_cfg: dict[str, Any]) -> dict[str, Any]:
    """Fill in `mode` from the AccessGrid template, for adapters that ask.

    Module-level because the engine is not the only caller: anything that
    builds an adapter to ask what it has done needs the same direction, and
    reading it from the stored params alone silently yields the default.
    """
    descriptor = get_descriptor(pacs_cfg.get("vendor", ""))
    if descriptor is None or not descriptor.derives_mode_from_template:
        return pacs_cfg

    protocol = template_protocol(ag, ag_cfg["template_id"])
    # An unreadable template must not silently flip a read-only integration
    # into one that writes cards into the PACS.
    mode = "seos" if protocol == PROTOCOL_SEOS else "desfire"
    if not protocol:
        logger.warning(
            "Could not read the card template's protocol — assuming %s", mode,
        )
    params = dict(pacs_cfg.get("params") or {})
    params["mode"] = mode
    return {**pacs_cfg, "params": params}


@dataclass
class CycleResult:
    started_at: str
    duration_ms: int
    provisioned: int = 0
    status_changes: int = 0
    deleted: int = 0
    ag_to_pacs: int = 0
    retried: int = 0
    field_updates: int = 0
    error: str | None = None
    # False for failures that retrying cannot fix and that a human is
    # already being asked about, or for a cycle deliberately abandoned.
    # Counting those toward the circuit breaker pauses the engine for a
    # reason nobody can act on from here.
    counts_as_failure: bool = True


@dataclass
class EngineStatus:
    running: bool = False
    paused: bool = False
    pause_reason: str = ""
    last_cycle: CycleResult | None = None
    last_error: str | None = None
    next_run_iso: str | None = None
    cached_interval_s: float = MIN_INTERVAL_S
    consecutive_errors: int = 0
    pacs_reachable: bool = True
    ag_reachable: bool = True
    # Set when the PACS session expired: only a human signing in clears it.
    reconnect_required: bool = False


class SyncEngine:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._trigger = threading.Event()
        self._cycle_lock = threading.Lock()
        self._status = EngineStatus()
        self._status_lock = threading.Lock()
        # (config fingerprint, adapter). Adapters are reused across cycles:
        # some hold a warm cache that is expensive to rebuild (Millennium
        # reads ~1800 detail pages to prime one) and an HTTP connection pool
        # that would otherwise leak a little on every cycle.
        self._pacs: tuple[str, Any] | None = None
        # Bumped whenever configuration changes. The running cycle checks it
        # between phases and bails out: it is holding an adapter built from
        # the old settings, and finishing the cycle would act on them.
        self._generation = 0

    # ----- public API --------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="sync-engine")
        self._thread.start()
        with self._status_lock:
            self._status.running = True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._trigger.set()  # wake from sleep
        if self._thread:
            self._thread.join(timeout=timeout)
        self._retire_pacs_adapter()
        with self._status_lock:
            self._status.running = False

    def trigger_now(self) -> None:
        """Run a cycle ASAP (will wait for the current cycle if one is running)."""
        self._trigger.set()

    def session_restored(self) -> None:
        """A new PACS session has been captured and proven to read.

        Clears the reconnect flag immediately. Otherwise the banner survives
        until the next cycle *finishes*, and a first cold sweep of a large
        install takes minutes — so the operator sits looking at a demand to
        do the thing they just did.
        """
        with self._status_lock:
            self._status.reconnect_required = False
            self._status.pacs_reachable = True
            self._status.consecutive_errors = 0
            self._status.paused = False
            self._status.pause_reason = ""
        self.invalidate_pacs_adapter()
        self._trigger.set()

    def invalidate_pacs_adapter(self) -> None:
        """Drop the cached adapter and abandon any cycle still using it.

        Needed when something the adapter read at construction changed
        without the connection settings changing — a recaptured PACS session,
        or a new enrollment trigger. The in-flight cycle matters as much as
        the next one: it holds an adapter built from the old settings, so
        left alone it would keep provisioning against the old trigger.
        """
        with self._status_lock:
            self._generation += 1
        self._retire_pacs_adapter()

    def get_status(self) -> dict[str, Any]:
        with self._status_lock:
            s = self._status
            return {
                "running": s.running,
                "paused": s.paused,
                "pause_reason": s.pause_reason,
                "last_cycle": asdict(s.last_cycle) if s.last_cycle else None,
                "last_error": s.last_error,
                "next_run_iso": s.next_run_iso,
                "cached_interval_s": s.cached_interval_s,
                "consecutive_errors": s.consecutive_errors,
                "pacs_reachable": s.pacs_reachable,
                "ag_reachable": s.ag_reachable,
                "reconnect_required": s.reconnect_required,
            }

    def run_cycle_blocking(self) -> CycleResult:
        """Run a single cycle in the calling thread. Used by manual triggers
        that want to surface the result immediately (e.g. CLI smoke test)."""
        return self._run_one_cycle()

    # ----- internals ---------------------------------------------------

    def _run_loop(self) -> None:
        logger.info("Sync engine starting")
        while not self._stop.is_set():
            with self._status_lock:
                consec = self._status.consecutive_errors

            if consec >= MAX_CONSECUTIVE_ERRORS:
                with self._status_lock:
                    self._status.paused = True
                    self._status.pause_reason = (
                        f"{consec} consecutive errors — pausing engine. "
                        "Fix the underlying issue then restart the service."
                    )
                logger.error(
                    "Sync engine paused after %d consecutive errors", consec
                )
                # Sleep in 5s chunks so a manual trigger or stop wakes us.
                while not self._stop.is_set():
                    if self._trigger.wait(timeout=5):
                        self._trigger.clear()
                        with self._status_lock:
                            self._status.paused = False
                            self._status.pause_reason = ""
                            self._status.consecutive_errors = 0
                        break
                continue

            result = self._run_one_cycle()
            with self._status_lock:
                self._status.last_cycle = result
                if result.error and result.counts_as_failure:
                    self._status.consecutive_errors += 1
                    self._status.last_error = result.error
                    sleep_s = ERROR_BACKOFF_S
                else:
                    self._status.consecutive_errors = 0
                    self._status.last_error = None
                    # Dynamic interval based on cycle wall time.
                    sleep_s = max(
                        MIN_INTERVAL_S,
                        min(MAX_INTERVAL_S, (result.duration_ms / 1000.0) * INTERVAL_MULTIPLIER),
                    )
                self._status.cached_interval_s = sleep_s
                self._status.next_run_iso = (
                    datetime.now(UTC).isoformat(timespec="seconds")
                )

            self._trigger.wait(timeout=sleep_s)
            self._trigger.clear()

    def _run_one_cycle(self) -> CycleResult:
        if not self._cycle_lock.acquire(blocking=False):
            logger.warning("Cycle already running — skipping")
            return CycleResult(
                started_at=datetime.now(UTC).isoformat(timespec="seconds"),
                duration_ms=0,
                error="cycle_in_flight",
            )
        try:
            return self._run_one_cycle_locked()
        finally:
            self._cycle_lock.release()

    def _run_one_cycle_locked(self) -> CycleResult:
        started_at = datetime.now(UTC).isoformat(timespec="seconds")
        start_ms = time.time()
        result = CycleResult(started_at=started_at, duration_ms=0)

        ag_cfg = AccessGridConfig.load()
        pacs_cfg = PacsConfig.load()
        if not ag_cfg or not pacs_cfg:
            result.error = "not_configured"
            result.duration_ms = int((time.time() - start_ms) * 1000)
            logger.warning("Sync skipped — wizard not yet completed")
            return result

        try:
            ag = build_ag_client(ag_cfg["account_id"], ag_cfg["api_secret"])
        except Exception as e:  # noqa: BLE001
            result.error = f"ag_init: {e}"
            result.duration_ms = int((time.time() - start_ms) * 1000)
            with self._status_lock:
                self._status.ag_reachable = False
            return result

        pacs_cfg = self._with_derived_mode(ag, ag_cfg, pacs_cfg)
        try:
            pacs = self._pacs_adapter(pacs_cfg)
        except Exception as e:  # noqa: BLE001
            result.error = f"pacs_init: {e}"
            result.duration_ms = int((time.time() - start_ms) * 1000)
            with self._status_lock:
                self._status.pacs_reachable = False
            return result

        try:
            snapshot = build_snapshot(pacs, ag, ag_cfg["template_id"])
            with self._status_lock:
                self._status.pacs_reachable = bool(snapshot.people)
                self._status.ag_reachable = True
                self._status.reconnect_required = False
        except PacsAuthExpired as e:
            return self._handle_auth_expired(pacs_cfg["vendor"], e, result, start_ms)
        except Exception as e:  # noqa: BLE001
            result.error = f"snapshot: {e}"
            result.duration_ms = int((time.time() - start_ms) * 1000)
            with self._status_lock:
                # Not an expired session — the PACS did not answer at all —
                # so this is a different banner and re-authenticating would
                # not help.
                self._status.pacs_reachable = False
            return result

        with self._status_lock:
            generation = self._generation

        def superseded() -> bool:
            with self._status_lock:
                return self._generation != generation

        site_code = (ag_cfg.get("site_code") or "").strip()
        dedupe = bool(ag_cfg.get("dedupe_by_site_card", False))
        extra_metadata = dict(ag_cfg.get("extra_metadata") or {})
        card_title = (ag_cfg.get("card_title") or "").strip()
        card_classification = (ag_cfg.get("card_classification") or "").strip()
        use_file_data = (
            (pacs_cfg.get("options") or {}).get("credential_encoding")
            == PacsConfig.ENCODING_FILE_DATA
        )
        try:
            result.provisioned = phase1_provision.run(
                snapshot, ag, ag_cfg["template_id"], site_code,
                dedupe_by_site_card=dedupe,
                extra_metadata=extra_metadata,
                use_file_data=use_file_data,
                card_title=card_title,
                card_classification=card_classification,
                pacs=pacs,
                should_stop=superseded,
            )
            if superseded():
                return self._abandon(result, start_ms)
            result.status_changes = phase2_local_to_ag.run(snapshot, ag)
            result.deleted = phase3_deletions.run(snapshot, ag)
            if superseded():
                return self._abandon(result, start_ms)
            result.ag_to_pacs = phase4_ag_to_local.run(snapshot, pacs)
            result.retried = phase5_retries.run(
                snapshot, ag, ag_cfg["template_id"], site_code,
                dedupe_by_site_card=dedupe,
                extra_metadata=extra_metadata,
                use_file_data=use_file_data,
                card_title=card_title,
                card_classification=card_classification,
                pacs=pacs,
            )
            result.field_updates = phase6_field_changes.run(snapshot, ag)
        except PacsAuthExpired as e:
            return self._handle_auth_expired(pacs_cfg["vendor"], e, result, start_ms)
        except Exception as e:  # noqa: BLE001
            result.error = f"phase: {e}"
            logger.exception("Phase failure during sync")
        finally:
            result.duration_ms = int((time.time() - start_ms) * 1000)

        logger.info(
            "Sync cycle complete in %dms: +%d new, %d status, -%d deleted, %d ag→pacs, %d field, %d retried",
            result.duration_ms, result.provisioned, result.status_changes,
            result.deleted, result.ag_to_pacs, result.field_updates, result.retried,
        )
        return result


    def _with_derived_mode(
        self, ag, ag_cfg: dict[str, Any], pacs_cfg: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve the PACS direction, logging when it changes.

        Resolved every cycle rather than stored at setup, so swapping the
        card template for one of a different technology takes effect on its
        own. The adapter cache is keyed on the config, so a change here
        rebuilds the adapter automatically.
        """
        resolved = derived_pacs_params(ag, ag_cfg, pacs_cfg)
        was = (pacs_cfg.get("params") or {}).get("mode")
        now = (resolved.get("params") or {}).get("mode")
        if now and was != now:
            logger.info("Running in %s mode", now)
        return resolved

    def _abandon(self, result: CycleResult, start_ms: float) -> CycleResult:
        """Stop a cycle whose configuration changed underneath it."""
        logger.warning(
            "Configuration changed mid-cycle — abandoning it and starting over"
        )
        result.error = "superseded"
        result.counts_as_failure = False
        result.duration_ms = int((time.time() - start_ms) * 1000)
        self._trigger.set()
        return result

    def _pacs_adapter(self, pacs_cfg: dict[str, Any]):
        """The adapter for this config, built once and reused."""
        fingerprint = json.dumps(pacs_cfg, sort_keys=True, default=str)
        if self._pacs is not None and self._pacs[0] == fingerprint:
            return self._pacs[1]
        self._retire_pacs_adapter()
        adapter = build_pacs_adapter(pacs_cfg["vendor"], pacs_cfg["params"])
        self._pacs = (fingerprint, adapter)
        return adapter

    def _retire_pacs_adapter(self) -> None:
        previous, self._pacs = self._pacs, None
        if previous is None:
            return
        close = getattr(previous[1], "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 — nothing useful to do on close
                logger.debug("Ignoring error closing the previous PACS adapter")

    def _handle_auth_expired(
        self, vendor: str, error: Exception, result: CycleResult, start_ms: float,
    ) -> CycleResult:
        """Park the cycle and ask a human to sign in again.

        Deliberately not counted toward the circuit breaker: the engine
        should keep waking up and clear the flag the moment a new session
        appears, rather than needing a restart after ten quiet retries.
        """
        from ..notifications import notify_reconnect_required

        descriptor = get_descriptor(vendor)
        name = descriptor.display_name if descriptor else vendor
        with self._status_lock:
            self._status.reconnect_required = True
            self._status.pacs_reachable = False
        result.error = f"pacs_auth_expired: {error}"
        # Retrying cannot fix a captcha-gated login, and the operator has
        # already been told. Pausing the engine on top of that would mean a
        # restart was needed after reconnecting.
        result.counts_as_failure = False
        result.duration_ms = int((time.time() - start_ms) * 1000)
        try:
            notify_reconnect_required(name, str(error))
        except Exception:  # noqa: BLE001 — notification must not mask the cause
            logger.exception("Failed to send the reconnect notification")
        return result


_engine: SyncEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> SyncEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = SyncEngine()
        return _engine
