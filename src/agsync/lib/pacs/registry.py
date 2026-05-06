"""Map vendor id → adapter factory.

To add a new PACS:
  1. Create lib/pacs/<vendor>/ with adapter.py
  2. Register the descriptor + factory below.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import PacsAdapter, PacsDescriptor

_DESCRIPTORS: dict[str, PacsDescriptor] = {}
_FACTORIES: dict[str, Callable[[dict[str, Any]], PacsAdapter]] = {}


def register(vendor: str, descriptor: PacsDescriptor, factory: Callable[[dict[str, Any]], PacsAdapter]) -> None:
    _DESCRIPTORS[vendor] = descriptor
    _FACTORIES[vendor] = factory


def available_pacs() -> list[PacsDescriptor]:
    return list(_DESCRIPTORS.values())


def get_descriptor(vendor: str) -> PacsDescriptor | None:
    return _DESCRIPTORS.get(vendor)


def build_adapter(vendor: str, params: dict[str, Any]) -> PacsAdapter:
    if vendor not in _FACTORIES:
        raise KeyError(f"Unknown PACS vendor: {vendor}")
    return _FACTORIES[vendor](params)


# ---------------------------------------------------------------------------
# Auto-register built-in adapters. Each module registers itself on import.
# Importing here keeps the public API simple: callers do
# `from agsync.lib.pacs import available_pacs` and get the full list.
# ---------------------------------------------------------------------------

from . import avigilon as _avigilon  # noqa: E402, F401
from . import lenel as _lenel  # noqa: E402, F401
