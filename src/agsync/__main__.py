"""CLI entrypoint.

Subcommands:
  run                — run the server in the foreground (useful for dev)
  install-service    — install as a Windows Service (Windows only, admin)
  uninstall-service  — remove the Windows Service (Windows only, admin)
  reset-admin        — wipe the admin user; the wizard will prompt for a
                       new one on the next page load
  generate-key       — print a new Fernet key for AG_SYNC_ENCRYPTION_KEY
"""

from __future__ import annotations

import logging
import socket
import sys
from pathlib import Path

import click
import uvicorn

from . import __version__


def _local_ips() -> list[str]:
    out: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            family, *_, sockaddr = info
            if family == socket.AF_INET:
                ip = sockaddr[0]
                if not ip.startswith("127."):
                    out.add(ip)
    except OSError:
        pass
    return sorted(out)


@click.group()
@click.version_option(__version__, prog_name="agsync")
def cli() -> None:
    """AccessGrid Sync — bridges PACS to AccessGrid."""


@cli.command()
@click.option("--host", default=None, help="Override AG_SYNC_HOST")
@click.option("--port", type=int, default=None, help="Override AG_SYNC_PORT")
def run(host: str | None, port: int | None) -> None:
    """Run the server in the foreground."""
    from .config import get_settings
    from .observability import init_sentry

    init_sentry()
    settings = get_settings()
    bind_host = host or settings.host
    bind_port = port or settings.port

    print(f"AccessGrid Sync v{__version__}")
    print(f"DB: {settings.db_path}")
    print("Web UI URLs:")
    print(f"  http://localhost:{bind_port}")
    for ip in _local_ips():
        print(f"  http://{ip}:{bind_port}")
    print()

    uvicorn.run(
        "agsync.server:create_app",
        factory=True,
        host=bind_host,
        port=bind_port,
        log_level=settings.log_level.lower(),
    )


@cli.command("reset-admin")
@click.confirmation_option(prompt="Wipe the admin user and re-run the wizard?")
def reset_admin_cmd() -> None:
    """Delete the admin user. Wizard re-runs on next request."""
    from .auth import delete_admin
    from .db import init_db

    init_db()
    delete_admin()
    click.echo("Admin user removed. Restart the service if it's running.")


@cli.command("generate-key")
def generate_key_cmd() -> None:
    """Print a new Fernet key. Set AG_SYNC_ENCRYPTION_KEY to its value."""
    from cryptography.fernet import Fernet
    click.echo(Fernet.generate_key().decode())


@cli.command("install-service")
def install_service_cmd() -> None:
    """Install as a Windows Service. Requires elevation."""
    if sys.platform != "win32":
        click.echo("install-service is Windows-only", err=True)
        sys.exit(1)
    from .service import install_service
    install_service()


@cli.command("uninstall-service")
def uninstall_service_cmd() -> None:
    """Remove the Windows Service. Requires elevation."""
    if sys.platform != "win32":
        click.echo("uninstall-service is Windows-only", err=True)
        sys.exit(1)
    from .service import uninstall_service
    uninstall_service()


def _bootstrap_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


if __name__ == "__main__":
    _bootstrap_logging()
    cli()
