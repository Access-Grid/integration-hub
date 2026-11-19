"""What the sync loop is working on right now, for the log of a failure.

Each phase names the pass it is on as it reaches it; if the phase then
crashes, the engine's error says where — phase and pass — without every loop
needing its own handler. One cycle runs at a time, so a ContextVar is enough.
"""

from __future__ import annotations

from contextvars import ContextVar

_phase: ContextVar[str] = ContextVar("sync_phase", default="")
_item: ContextVar[str] = ContextVar("sync_item", default="")


def phase(name: str) -> None:
    _phase.set(name)
    _item.set("")


def at(item: str) -> None:
    _item.set(item)


def where() -> str:
    """'phase 2 (status push), on Jim Cooper a/t1 (pass X)' or what is known of it."""
    name, item = _phase.get(), _item.get()
    if not name:
        return "before any phase"
    return f"{name}, on {item}" if item else name
