"""Make Python validate outbound TLS against the OS trust store.

Python does not use the operating system's certificate store; it ships its own
bundle (certifi). On a corporate network whose SSL-inspection appliance
re-signs HTTPS with a private root CA, that CA is installed into the Windows
trust store — so Edge, curl and the Windows service all trust it — but Python
has never heard of it and rejects every connection with "self-signed
certificate in certificate chain". The hub then can't reach AccessGrid even
though the browser on the same box can.

`truststore.inject_into_ssl()` replaces the default SSLContext so the standard
library (and anything on it: httpx, the AccessGrid SDK, smtplib) verifies
against the OS store instead. Whatever the machine already trusts, the hub now
trusts too — including the inspection CA — with no bundle to maintain.

Call `use_os_trust_store()` once, as early as a run path starts, before any TLS
connection is made. It is idempotent and never raises: if truststore is missing
or the platform is unsupported, the hub falls back to certifi rather than
failing to start.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_injected = False


def use_os_trust_store() -> bool:
    """Route TLS verification through the OS trust store. Returns whether it took.

    Safe to call more than once; only the first call injects.
    """
    global _injected
    if _injected:
        return True
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception as e:  # pragma: no cover - defensive; absence must not crash
        logger.warning("Could not use the OS trust store, falling back to certifi: %s", e)
        return False
    _injected = True
    logger.debug("TLS verification is using the OS trust store")
    return True
