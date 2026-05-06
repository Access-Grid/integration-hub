"""Self-signed TLS cert for the local web UI.

The NUC runs on a private LAN with no DNS or PKI; we ship a self-signed
cert, generated on first boot and reused thereafter, so the management
UI can serve HTTPS without operator intervention. Browsers will warn
once and the operator clicks through. The cert is regenerated if it
expires or is missing.

Cert + key live next to the SQLite DB (`<db dir>/tls/`), inheriting the
same per-user filesystem permissions as the credential store.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .config import get_settings

logger = logging.getLogger(__name__)

CERT_VALIDITY_DAYS = 825  # max accepted by modern browsers without warnings beyond self-signed
RENEW_THRESHOLD_DAYS = 30


def _tls_dir() -> Path:
    return get_settings().db_path.parent / "tls"


def _cert_path() -> Path:
    return _tls_dir() / "cert.pem"


def _key_path() -> Path:
    return _tls_dir() / "key.pem"


def _local_ips() -> list[str]:
    out: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            family, *_, sockaddr = info
            if family == socket.AF_INET:
                out.add(sockaddr[0])
    except OSError:
        pass
    out.add("127.0.0.1")
    return sorted(out)


def _build_san() -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = [x509.DNSName("localhost")]
    try:
        hostname = socket.gethostname()
        if hostname:
            entries.append(x509.DNSName(hostname))
    except OSError:
        pass
    for ip in _local_ips():
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            continue
    return x509.SubjectAlternativeName(entries)


def _generate(cert_path: Path, key_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "AGSyncTool"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AccessGrid"),
        ]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(_build_san(), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    logger.info("TLS: generated self-signed cert at %s", cert_path)


def _is_usable(cert_path: Path) -> bool:
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return False
    expires = cert.not_valid_after_utc
    return expires - datetime.now(UTC) > timedelta(days=RENEW_THRESHOLD_DAYS)


def ensure_cert() -> tuple[Path, Path]:
    """Return (cert, key) paths. Generates on first call or if expired."""
    cert_path = _cert_path()
    key_path = _key_path()
    if not (cert_path.exists() and key_path.exists() and _is_usable(cert_path)):
        _generate(cert_path, key_path)
    return cert_path, key_path
