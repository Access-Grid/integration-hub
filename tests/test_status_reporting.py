"""Reporting the hub's own health to AccessGrid.

A hub runs unattended, and the failures that matter most are the quiet ones
— a stopped engine, or an expired PACS session that needs a human. These
tests cover the report's shape, and rather more carefully, the promise that
reporting can never disturb the sync it reports on.
"""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from agsync.status import fingerprint, payload, reporter, transport

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Never let these tests reach the operator's real install.

    `get_settings()` defaults db_path to the live database, so anything
    that resolves configuration would read a real account's credentials —
    and open a file that applies migrations when touched.
    """
    monkeypatch.setenv("AG_SYNC_DB_PATH", str(tmp_path / "status.db"))
    from agsync.config import get_settings
    get_settings.cache_clear()
    from agsync.db import connection
    connection._conn = None  # type: ignore[attr-defined]
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_fingerprint():
    fingerprint._reset_for_tests()
    yield
    fingerprint._reset_for_tests()


def test_the_id_is_stable_across_calls():
    assert fingerprint.instance_id() == fingerprint.instance_id()


def test_two_machines_get_different_ids(monkeypatch):
    monkeypatch.setattr(fingerprint, "hostname", lambda: "MILLENNIUM-PC")
    monkeypatch.setattr(fingerprint, "machine_id", lambda: "guid-a")
    first = fingerprint.instance_id()

    fingerprint._reset_for_tests()
    monkeypatch.setattr(fingerprint, "hostname", lambda: "FRONT-DESK")
    monkeypatch.setattr(fingerprint, "machine_id", lambda: "guid-b")

    assert fingerprint.instance_id() != first


def test_an_os_update_does_not_change_the_id(monkeypatch):
    """The reason os_version is a field and not hash input.

    A Windows update bumping the build number must not present as a brand
    new install that has never synced.
    """
    monkeypatch.setattr(fingerprint.platform, "version", lambda: "10.0.19045")
    before = fingerprint.instance_id()

    fingerprint._reset_for_tests()
    monkeypatch.setattr(fingerprint.platform, "version", lambda: "10.0.22631")

    assert fingerprint.instance_id() == before


def test_a_machine_with_no_readable_id_is_still_deterministic(monkeypatch):
    """It must degrade, never invent.

    `uuid.getnode()` is the trap here: it silently returns a *random* value
    when it cannot read a NIC, which would re-identify the box on every
    restart. Nothing random may reach the hash.
    """
    monkeypatch.setattr(fingerprint, "_machine_id", None)
    monkeypatch.setattr(
        fingerprint, "_linux_machine_id", lambda: (_ for _ in ()).throw(OSError("nope"))
    )
    monkeypatch.setattr(fingerprint, "_macos_machine_id", lambda: "")
    monkeypatch.setattr(fingerprint, "_windows_machine_id", lambda: "")

    first = fingerprint.instance_id()
    fingerprint._reset_for_tests()
    monkeypatch.setattr(fingerprint, "_macos_machine_id", lambda: "")

    assert fingerprint.instance_id() == first
    assert first  # still produced one


def test_the_instance_block_is_complete():
    described = fingerprint.describe()
    assert set(described) == {
        "id", "hostname", "agent_version", "platform", "os_version", "arch",
    }
    assert all(described.values()), "no blank fields"


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------

HEALTHY = {
    "running": True, "paused": False, "pause_reason": "",
    "last_cycle": {
        "started_at": "2026-10-06T14:21:32+00:00", "duration_ms": 16210,
        "error": None, "provisioned": 0, "status_changes": 1, "deleted": 0,
        "ag_to_pacs": 2, "field_updates": 0, "retried": 0,
    },
    "last_error": None, "next_run_iso": "2026-10-06T14:22:32+00:00",
    "cached_interval_s": 60.0, "consecutive_errors": 0,
    "pacs_reachable": True, "ag_reachable": True, "reconnect_required": False,
    "last_pacs_read_iso": "2026-10-06T14:21:48+00:00",
}

TOTALS = {"pacs_people": 143, "tracked_credentials": 38, "active": 35,
          "pending": 2, "failed": 0}


def _build(status=None, **kw):
    merged = {**HEALTHY, **(status or {})}
    opts = {
        "instance": fingerprint.describe(), "uptime_s": 48213,
        "vendor": "millennium_ultra", "display_name": "Millennium Ultra",
        "writes_credentials": True, "totals": TOTALS,
    }
    opts.update(kw)
    return payload.build(merged, **opts)


def test_a_healthy_report_has_no_conditions():
    assert _build()["conditions"] == []


def test_every_timestamp_is_utc_with_a_z():
    report = _build()
    stamps = [
        report["sent_at"],
        report["pacs"][0]["last_successful_read_at"],
        report["sync"]["next_run_at"],
        report["sync"]["last_cycle"]["started_at"],
    ]
    assert all(s.endswith("Z") for s in stamps), stamps
    assert all("+00:00" not in s for s in stamps)


def test_the_pacs_block_is_a_list():
    """One connection today; modelled as an array so a second needs no v2."""
    assert isinstance(_build()["pacs"], list)
    assert len(_build()["pacs"]) == 1
    assert _build(vendor="")["pacs"] == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({}, payload.CONNECTED),
        ({"reconnect_required": True}, payload.SESSION_EXPIRED),
        ({"pacs_reachable": False}, payload.UNREACHABLE),
    ],
)
def test_connection_status_enum(status, expected):
    assert _build(status)["pacs"][0]["connection_status"] == expected


def test_an_expired_session_outranks_unreachable():
    """Both are true at once, and only one tells the operator what to do."""
    report = _build({"reconnect_required": True, "pacs_reachable": False})

    assert report["pacs"][0]["connection_status"] == payload.SESSION_EXPIRED
    assert "pacs_session_expired" in report["conditions"]


def test_an_unconfigured_hub_says_so():
    assert _build(vendor="", display_name="", writes_credentials=None)["pacs"] == []


@pytest.mark.parametrize(("writes", "expected"), [
    (True, payload.AG_TO_PACS),
    (False, payload.PACS_TO_AG),
    # Null until an adapter has been built and asked: the first beats after
    # a restart, and any cycle that failed before constructing one.
    (None, None),
])
def test_direction_comes_from_what_the_adapter_does(writes, expected):
    """Not from the protocol it speaks.

    A DESFire integration that writes into the PACS runs in the same
    direction as a Seos one, so the adapter answers this for itself rather
    than having a protocol name mapped onto it.
    """
    assert _build(writes_credentials=writes)["pacs"][0]["direction"] == expected



@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({}, payload.RUNNING),
        ({"paused": True}, payload.PAUSED),
        ({"running": False}, payload.STOPPED),
    ],
)
def test_sync_state_enum(status, expected):
    assert _build(status)["sync"]["state"] == expected


def test_a_restart_reads_as_never_synced_not_as_an_outage():
    report = _build({"last_cycle": None})

    assert report["sync"]["last_cycle"] is None
    assert "never_synced" in report["conditions"]


def test_a_failed_cycle_is_not_ok():
    report = _build({"last_cycle": {**HEALTHY["last_cycle"], "error": "phase: boom"}})

    assert report["sync"]["last_cycle"]["ok"] is False
    assert report["sync"]["last_cycle"]["error"] == "phase: boom"


def test_the_write_counter_is_not_named_after_a_direction():
    """The report uses "ag_to_pacs" for which way credentials flow, so the
    count of credentials written cannot share the word."""
    cycle = _build()["sync"]["last_cycle"]

    assert cycle["credentials_written"] == 2
    assert "ag_to_pacs" not in cycle
    # Still a direction elsewhere in the same payload, which is the point.
    assert _build()["pacs"][0]["direction"] == payload.AG_TO_PACS


def test_stuck_rows_raise_a_condition():
    assert "provision_failures" in _build(totals={**TOTALS, "failed": 3})["conditions"]


def test_the_schema_is_declared():
    assert _build()["schema"] == payload.SCHEMA


# ---------------------------------------------------------------------------
# Nothing sensitive leaves the building
# ---------------------------------------------------------------------------


def test_free_text_is_scrubbed_of_infrastructure():
    dirty = (
        "snapshot: failed reading https://hosted8.mgiaccess.com/Cardholders "
        r"from C:\Users\Millennium\AppData\app.db and /Users/ab/Projects/x.py"
    )
    clean = payload.scrub(dirty)

    assert "mgiaccess" not in clean
    assert "AppData" not in clean
    assert "Projects" not in clean
    assert "[url]" in clean and "[path]" in clean


def test_free_text_is_capped():
    assert len(payload.scrub("x" * 5000)) <= 200


def test_a_cycle_error_is_scrubbed_on_the_way_out():
    report = _build({
        "last_cycle": {**HEALTHY["last_cycle"], "error": "https://host/secret-path"},
    })
    assert "host" not in report["sync"]["last_cycle"]["error"]


def test_a_pause_reason_is_scrubbed_on_the_way_out():
    report = _build({"paused": True, "pause_reason": "stuck at https://host/x"})
    assert "host" not in report["sync"]["pause_reason"]


def test_no_cardholder_data_is_present():
    """Counts only. The report describes the install, not the people in it."""
    body = json.dumps(_build()).lower()
    for forbidden in ("email", "phone", "card_number", "full_name", "@"):
        assert forbidden not in body, forbidden


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def test_the_signature_is_over_the_exact_body_sent():
    """Re-serialising would invalidate it, so the bytes must match."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["sig"] = request.headers["X-PAYLOAD-SIG"]
        seen["acct"] = request.headers["X-ACCT-ID"]
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport.send(
        {"b": 2, "a": 1}, account_id="acct", secret="shh",
        base_url="https://example.test", client=client,
    )

    assert seen["acct"] == "acct"
    assert seen["sig"] == transport.sign(seen["body"].decode(), "shh")


def test_a_pasted_secret_with_whitespace_still_signs():
    """Confirmed against the dev endpoint: without the strip this is a 401
    indistinguishable from a wrong key, which is a miserable thing to
    debug."""
    body = '{"schema":1}'

    assert transport.sign(body, "shh\n") == transport.sign(body, "shh")
    assert transport.sign(body, "  shh  ") == transport.sign(body, "shh")


def test_the_signature_covers_the_body():
    """Tampering after signing must not go unnoticed. The dev endpoint
    answers 401 to exactly this."""
    assert transport.sign('{"a":1}', "k") != transport.sign('{"a":1} ', "k")


def test_no_content_is_not_an_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    assert transport.send({}, account_id="a", secret="s",
                          base_url="https://x.test", client=client) == {}


def test_a_rejection_carries_its_code_and_retry_after():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": "90"})))

    with pytest.raises(transport.StatusRejected) as caught:
        transport.send({}, account_id="a", secret="s",
                       base_url="https://x.test", client=client)

    assert caught.value.status_code == 429
    assert caught.value.retry_after == 90.0


def test_a_junk_response_body_is_ignored():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="<html>nope")))
    assert transport.send({}, account_id="a", secret="s",
                          base_url="https://x.test", client=client) == {}


def test_the_confirmed_endpoint_is_used():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport.send({}, account_id="a", secret="s",
                   base_url=transport.STAGING_BASE_URL, client=client)

    assert seen["url"] == (
        "https://staging-api.accessgrid.com/v1/console/integration-hub/status"
    )


def test_both_environments_are_named_not_typed():
    assert transport.PRODUCTION_BASE_URL == "https://api.accessgrid.com"
    assert transport.STAGING_BASE_URL == "https://staging-api.accessgrid.com"


def test_the_timeout_is_shorter_than_the_shortest_interval():
    """So a slow endpoint can never cause beats to queue behind each other."""
    assert transport.TIMEOUT.read < reporter.MIN_INTERVAL_S
    assert transport.TIMEOUT.connect < reporter.MIN_INTERVAL_S


# ---------------------------------------------------------------------------
# The reporter thread — it must not be able to hurt anything
# ---------------------------------------------------------------------------


class FakeEngine:
    def __init__(self, status=None):
        self._status = {**HEALTHY, "totals": dict(TOTALS),
                        "pacs_vendor": "millennium_ultra",
                        "pacs_display_name": "Millennium Ultra",
                        "pacs_writes_credentials": True}
        self._status.update(status or {})
        self.reads = 0

    def get_status(self):
        self.reads += 1
        return dict(self._status)


@pytest.fixture
def configured(monkeypatch):
    """A reporter whose credentials resolve without touching the database."""
    def _make(engine=None, **kw):
        rep = reporter.StatusReporter(engine or FakeEngine(), **kw)
        rep._cfg = {"account_id": "acct", "api_secret": "shh"}
        rep._cfg_read_at = time.time()
        return rep
    return _make


def test_a_beat_sends_one_report(configured, monkeypatch):
    sent = []
    monkeypatch.setattr(transport, "send", lambda report, **kw: sent.append(report) or {})

    rep = configured()
    assert rep._beat() == reporter.DEFAULT_INTERVAL_S
    assert len(sent) == 1
    assert sent[0]["pacs"][0]["vendor"] == "millennium_ultra"
    assert sent[0]["pacs"][0]["direction"] == payload.AG_TO_PACS


def test_an_unconfigured_hub_sends_nothing(monkeypatch):
    monkeypatch.setattr(
        transport, "send",
        lambda *a, **k: pytest.fail("must not post without credentials"),
    )
    rep = reporter.StatusReporter(FakeEngine())
    rep._cfg = None
    rep._cfg_read_at = time.time()

    assert rep._beat() == reporter.DEFAULT_INTERVAL_S


@pytest.mark.parametrize("boom", [
    httpx.ConnectError("refused"),
    httpx.ReadTimeout("hung"),
    transport.StatusRejected(500),
    RuntimeError("something nobody predicted"),
    KeyError("missing"),
    ValueError("nonsense"),
])
def test_no_failure_escapes_a_beat(configured, monkeypatch, boom):
    """The whole point. A beat returns a number; it never raises."""
    def explode(*a, **k):
        raise boom
    monkeypatch.setattr(transport, "send", explode)

    assert isinstance(configured()._beat(), float)


def test_failures_back_off_then_hold(configured, monkeypatch):
    monkeypatch.setattr(
        transport, "send",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")),
    )
    rep = configured()

    waits = [rep._beat() for _ in range(7)]

    assert waits[: len(reporter.BACKOFF_S)] == list(reporter.BACKOFF_S)
    assert waits[-1] == reporter.BACKOFF_S[-1], "holds, never grows forever"


def test_one_success_clears_the_backoff(configured, monkeypatch):
    rep = configured()
    monkeypatch.setattr(
        transport, "send",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("x")),
    )
    rep._beat()
    rep._beat()

    monkeypatch.setattr(transport, "send", lambda *a, **k: {})
    assert rep._beat() == reporter.DEFAULT_INTERVAL_S


@pytest.mark.parametrize("code", sorted(reporter.HARD_FAILURES))
def test_a_rejected_endpoint_is_not_hammered(configured, monkeypatch, code):
    """404 in particular: the path may simply not exist yet."""
    monkeypatch.setattr(
        transport, "send",
        lambda *a, **k: (_ for _ in ()).throw(transport.StatusRejected(code)),
    )
    assert configured()._beat() == reporter.HARD_BACKOFF_S


def test_retry_after_is_honoured_but_clamped(configured, monkeypatch):
    monkeypatch.setattr(
        transport, "send",
        lambda *a, **k: (_ for _ in ()).throw(
            transport.StatusRejected(429, retry_after=99999)),
    )
    assert configured()._beat() == 900.0


@pytest.mark.parametrize(("advised", "expected"), [
    (60, 60.0),
    (1, reporter.MIN_INTERVAL_S),        # cannot become a hot loop
    (99999, reporter.MAX_INTERVAL_S),    # cannot be switched off
    ("nonsense", reporter.DEFAULT_INTERVAL_S),
    (None, reporter.DEFAULT_INTERVAL_S),
])
def test_a_server_advised_interval_is_clamped(configured, monkeypatch, advised, expected):
    monkeypatch.setattr(
        transport, "send", lambda *a, **k: {"report_interval_s": advised}
    )
    assert configured()._beat() == expected


def test_a_beat_touches_no_database(configured, monkeypatch):
    """The rule that keeps the reporter off the sync thread's toes.

    Counts come from the engine's cached snapshot and the credentials are
    cached, so a beat is memory plus one POST. Proven by making any database
    access an error.
    """
    from agsync.db import connection

    monkeypatch.setattr(
        connection, "get_db",
        lambda *a, **k: pytest.fail("a beat must not reach the database"),
    )
    monkeypatch.setattr(transport, "send", lambda *a, **k: {})

    assert configured()._beat() == reporter.DEFAULT_INTERVAL_S


def test_an_unconfigured_hub_does_not_poll_the_database_either(monkeypatch):
    """The likeliest case, and the one that was wrong.

    Caching on the value rather than on when it was read meant "not
    configured" — which is None — never counted as cached, so a hub nobody
    had finished setting up went back to the database fifteen times a
    minute, forever.
    """
    rep = reporter.StatusReporter(FakeEngine())
    rep._cfg = None
    rep._cfg_read_at = time.time()   # looked, found nothing, recently

    from agsync.db import connection
    monkeypatch.setattr(
        connection, "get_db",
        lambda *a, **k: pytest.fail("an unconfigured beat must not poll the database"),
    )

    assert rep._beat() == reporter.DEFAULT_INTERVAL_S


def test_the_engine_is_never_locked_while_sending(monkeypatch):
    """A slow endpoint must not be able to stall a sync cycle.

    `get_status()` copies under the engine's lock and returns; the send
    happens afterwards. If that ever inverted, a hung POST would hold the
    lock and the cycle would block on its own status update.
    """
    from agsync.sync.engine import SyncEngine

    engine = SyncEngine()
    in_flight = threading.Event()
    release = threading.Event()

    def slow_send(*a, **k):
        in_flight.set()
        release.wait(timeout=5)
        return {}

    monkeypatch.setattr(transport, "send", slow_send)
    rep = reporter.StatusReporter(engine)
    rep._cfg = {"account_id": "a", "api_secret": "s"}
    rep._cfg_read_at = time.time()

    beat = threading.Thread(target=rep._beat, daemon=True)
    beat.start()
    assert in_flight.wait(timeout=5), "send never started"

    # Mid-send, the engine's own status must still be readable.
    done = threading.Event()
    threading.Thread(
        target=lambda: (engine.get_status(), done.set()), daemon=True
    ).start()
    assert done.wait(timeout=2), "the reporter was holding the engine's lock"

    release.set()
    beat.join(timeout=5)


def test_the_thread_is_a_daemon_and_stops_promptly(monkeypatch):
    """It must never hold the process open at shutdown."""
    monkeypatch.setattr(transport, "send", lambda *a, **k: {})
    rep = reporter.StatusReporter(FakeEngine())
    rep._cfg = None
    rep._cfg_read_at = time.time()

    rep.start()
    try:
        assert rep._thread.daemon is True
        assert rep._thread.is_alive()
    finally:
        rep.stop(timeout=3)

    assert not rep._thread.is_alive(), "stop() left the thread running"


def test_the_interval_is_clamped_at_construction():
    assert reporter.StatusReporter(FakeEngine(), interval_s=1)._interval_s == 15
    assert reporter.StatusReporter(FakeEngine(), interval_s=99999)._interval_s == 300


def test_reporting_is_off_until_the_endpoint_is_confirmed():
    """Deliberate: an unconfirmed path would 404 from every install."""
    from agsync.config import Settings

    assert Settings(encryption_key="x").status_reporting is False
