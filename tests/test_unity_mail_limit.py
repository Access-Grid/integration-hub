"""Avigilon Unity never sends more than 6 emails in any 24 hours, whatever happens."""

import importlib
import threading

import pytest

from agsync import notifications
from agsync.settings_store import NotificationConfig, PacsConfig

DAY = 24 * 60 * 60


class Relay:
    """smtplib.SMTP standing in for the relay: records what it is handed."""

    def __init__(self):
        self.sent, self.fail = [], False
        self._lock = threading.Lock()
        relay = self

        class SMTP:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self, **kw):
                pass

            def login(self, *a):
                pass

            def send_message(self, message):
                if relay.fail:
                    raise OSError("relay refused")
                with relay._lock:
                    relay.sent.append(message)

        self.SMTP = SMTP


@pytest.fixture
def relay(monkeypatch):
    notifications.reset_throttle()
    fake = Relay()
    monkeypatch.setattr(notifications.smtplib, "SMTP", fake.SMTP)
    NotificationConfig.save(smtp_host="smtp.example.com", use_starttls=False)
    PacsConfig.save("avigilon", {"host": "unity", "notify_email": "security@example.com"})
    yield fake
    notifications.reset_throttle()


@pytest.fixture
def clock(monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr(notifications.time, "time", lambda: now[0])
    return now


def _problems(n, start=0):
    return [notifications.notify_card_problem(f"p{i}", f"problem {i}") for i in range(start, start + n)]


def test_twenty_problems_send_six_emails_and_the_sixth_says_so(relay, clock, caplog):
    assert _problems(20) == [True] * 6 + [False] * 14
    assert len(relay.sent) == 6
    assert "email 6 of 6" in relay.sent[5].get_content()
    assert "email 6 of 6" not in relay.sent[4].get_content()
    assert caplog.text.count("Mail limit: 6 emails sent in the last 24 hours") == 14


def test_a_restart_does_not_reset_the_limit(relay, clock):
    _problems(6)
    importlib.reload(notifications)  # a restart: every in-memory record is gone
    clock[0] += 60
    assert _problems(6, start=100) == [False] * 6
    assert len(relay.sent) == 6


def test_mail_resumes_a_day_after_the_first_email_and_held_problems_are_not_lost(relay, clock):
    _problems(6)
    clock[0] += 3600
    assert _problems(1, start=6) == [False]  # held by the limit, not by its own throttle
    clock[0] += DAY - 3600 + 1
    assert _problems(1, start=6) == [True]
    assert len(relay.sent) == 7


def test_the_window_rolls_rather_than_resetting_at_midnight(relay, clock):
    for _ in range(6):
        _problems(1, start=int(clock[0]))
        clock[0] += 3 * 3600  # one every 3 hours
    assert len(relay.sent) == 6
    # 18 h after the first email: all six are within the last 24 hours.
    assert _problems(1, start=999) == [False]
    clock[0] += 6 * 3600 + 1  # the first email is now more than a day old
    assert _problems(1, start=1000) == [True]


def test_the_reconnect_email_counts_toward_the_same_limit(relay, clock):
    _problems(5)
    assert notifications.notify_reconnect_required("Avigilon Unity")
    assert _problems(1, start=50) == [False]
    assert len(relay.sent) == 6


def test_a_relay_that_errors_still_uses_a_slot(relay, clock):
    relay.fail = True
    assert _problems(6) == [False] * 6
    relay.fail = False
    assert _problems(1, start=10) == [False]
    assert relay.sent == []


def test_no_relay_configured_uses_no_slot(relay, clock):
    NotificationConfig.save(smtp_host="")
    _problems(10)
    NotificationConfig.save(smtp_host="smtp.example.com", use_starttls=False)
    notifications.reset_throttle()
    assert _problems(6, start=100) == [True] * 6


def test_a_record_that_cannot_be_read_sends_nothing(relay, clock, monkeypatch):
    def broken(key):
        raise RuntimeError("database locked")

    monkeypatch.setattr(notifications, "get_json", broken)
    assert _problems(3) == [False] * 3
    assert relay.sent == []


def test_a_clock_set_far_back_cannot_block_mail_for_good(relay, clock):
    _problems(6)
    clock[0] -= 30 * DAY
    assert _problems(1, start=10) == [True]


def test_many_threads_at_once_still_send_six(relay, clock):
    results = []
    threads = [threading.Thread(target=lambda i=i: results.append(
        notifications.notify_card_problem(f"t{i}", "problem"))) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 6
    assert len(relay.sent) == 6


def test_other_pacs_are_not_limited(relay, clock):
    PacsConfig.save("millennium_ultra", {"notify_email": "security@example.com"})
    assert _problems(10) == [True] * 10
