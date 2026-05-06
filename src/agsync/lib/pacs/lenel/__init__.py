"""Lenel OnGuard adapter — interface stub.

Concrete implementation deferred. The descriptor is registered so the
wizard surfaces Lenel as a choice; selecting it raises
NotImplementedError at adapter build time.
"""

from ..base import ConnectionField, PacsDescriptor
from ..registry import register
from .adapter import LenelAdapter

DESCRIPTOR = PacsDescriptor(
    vendor="lenel",
    display_name="Lenel OnGuard",
    trigger_help_key="pacs.lenel.trigger_help",
    connection_fields=[
        ConnectionField("server", "pacs.lenel.server"),
        ConnectionField("database", "pacs.lenel.database"),
        ConnectionField("username", "pacs.lenel.username"),
        ConnectionField("password", "pacs.lenel.password", kind="password"),
    ],
)

register("lenel", DESCRIPTOR, lambda params: LenelAdapter(**params))

__all__ = ["LenelAdapter", "DESCRIPTOR"]
