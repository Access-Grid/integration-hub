"""use_os_trust_store() must inject truststore once, and never crash when it is
missing — the hub has to start either way.
"""

import sys
import types

import agsync.trust as trust


def _fake_truststore(calls):
    mod = types.ModuleType("truststore")
    mod.inject_into_ssl = lambda: calls.append(1)
    return mod


def test_injects_once_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(trust, "_injected", False)
    calls: list[int] = []
    monkeypatch.setitem(sys.modules, "truststore", _fake_truststore(calls))

    assert trust.use_os_trust_store() is True
    assert trust.use_os_trust_store() is True  # second call is a no-op
    assert calls == [1]  # inject_into_ssl ran exactly once


def test_missing_truststore_does_not_crash(monkeypatch):
    monkeypatch.setattr(trust, "_injected", False)
    monkeypatch.setitem(sys.modules, "truststore", None)  # forces ImportError

    assert trust.use_os_trust_store() is False  # fell back to certifi, no raise


def test_inject_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(trust, "_injected", False)
    mod = types.ModuleType("truststore")

    def boom():
        raise RuntimeError("unsupported platform")

    mod.inject_into_ssl = boom
    monkeypatch.setitem(sys.modules, "truststore", mod)

    assert trust.use_os_trust_store() is False
