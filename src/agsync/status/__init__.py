"""Reporting this hub's own health to AccessGrid.

A hub runs unattended on a customer's machine, and until now the only way
to learn that it had stopped — or that its PACS session had expired, which
needs a human — was for somebody to notice that nothing was syncing. This
sends a small, regular report so that is visible from the outside.

It carries no cardholder data: counts, states and timestamps only.
"""

from __future__ import annotations

from .fingerprint import instance_id
from .payload import SCHEMA, build
from .reporter import DEFAULT_INTERVAL_S, StatusReporter

__all__ = [
    "DEFAULT_INTERVAL_S",
    "SCHEMA",
    "StatusReporter",
    "build",
    "instance_id",
]
