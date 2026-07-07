"""CDVI Atrium (on-prem) adapter."""

from ..base import ConnectionField, PacsDescriptor
from ..registry import register
from .adapter import CdviAdapter

DESCRIPTOR = PacsDescriptor(
    vendor="cdvi",
    display_name="CDVI Atrium (On-Prem)",
    trigger_help_key="pacs.cdvi.trigger_help",
    connection_fields=[
        ConnectionField("base_url", "pacs.cdvi.base_url", kind="url", placeholder="https://192.168.1.50"),
        ConnectionField("username", "pacs.cdvi.username"),
        ConnectionField("password", "pacs.cdvi.password", kind="password"),
    ],
)

register("cdvi", DESCRIPTOR, lambda params: CdviAdapter(**params))

__all__ = ["CdviAdapter", "DESCRIPTOR"]
