"""DMP System Link (on-prem, read-only) adapter."""

from ..base import ConnectionField, PacsDescriptor
from ..registry import register
from .adapter import DmpAdapter

DESCRIPTOR = PacsDescriptor(
    vendor="dmp",
    display_name="DMP System Link (On-Prem)",
    trigger_help_key="pacs.dmp.trigger_help",
    connection_fields=[
        ConnectionField(
            "db_path", "pacs.dmp.db_path",
            placeholder=r"C:\ProgramData\DMP\SystemLink\Database",
        ),
        ConnectionField("encryption_key", "pacs.dmp.encryption_key", kind="password"),
        ConnectionField(
            "email_field", "pacs.dmp.email_field",
            required=False, placeholder="U_FIELD1",
        ),
    ],
    # DMP has no pre-encoded credential blob we transmit; we send the decoded
    # site_code + card number, so no file_data option in the wizard.
    supports_file_data=False,
)

register("dmp", DESCRIPTOR, lambda params: DmpAdapter(**params))

__all__ = ["DmpAdapter", "DESCRIPTOR"]
