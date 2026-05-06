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
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from ..ag import build_client as build_ag_client
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


class SyncEngine:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._trigger = threading.Event()
        self._cycle_lock = threading.Lock()
        self._status = EngineStatus()
        self._status_lock = threading.Lock()

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
        with self._status_lock:
            self._status.running = False

    def trigger_now(self) -> None:
        """Run a cycle ASAP (will wait for the current cycle if one is running)."""
        self._trigger.set()

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
                if result.error:
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

        try:
            pacs = build_pacs_adapter(pacs_cfg["vendor"], pacs_cfg["params"])
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
        except Exception as e:  # noqa: BLE001
            result.error = f"snapshot: {e}"
            result.duration_ms = int((time.time() - start_ms) * 1000)
            return result

        site_code = (ag_cfg.get("site_code") or "").strip()
        try:
            result.provisioned = phase1_provision.run(
                snapshot, ag, ag_cfg["template_id"], site_code,
            )
            result.status_changes = phase2_local_to_ag.run(snapshot, ag)
            result.deleted = phase3_deletions.run(snapshot, ag)
            result.ag_to_pacs = phase4_ag_to_local.run(snapshot, pacs)
            result.retried = phase5_retries.run(
                snapshot, ag, ag_cfg["template_id"], site_code,
            )
            result.field_updates = phase6_field_changes.run(snapshot, ag)
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


_engine: SyncEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> SyncEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = SyncEngine()
        return _engine
