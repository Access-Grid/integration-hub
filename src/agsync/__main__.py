"""CLI entrypoint.

Subcommands:
  run                — run the server in the foreground (useful for dev)
  install-service    — install as a Windows Service (Windows only, admin)
  uninstall-service  — remove the Windows Service (Windows only, admin)
  reset-admin        — wipe the admin user; the wizard will prompt for a
                       new one on the next page load
  generate-key       — print a new Fernet key for AG_SYNC_ENCRYPTION_KEY
  register-uri       — register the agconnect:// handler for this user
  unregister-uri     — remove it
  connect            — AG Connect side-car; normally started by the OS when
                       the operator clicks an agconnect:// link, not by hand
"""

from __future__ import annotations

import logging
import socket
import sys

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


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="agsync")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """AccessGrid Sync — bridges PACS to AccessGrid.

    With no subcommand, starts the server (so the .exe is double-clickable).
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(run, host=None, port=None)


@cli.command()
@click.option("--host", default=None, help="Override AG_SYNC_HOST")
@click.option("--port", type=int, default=None, help="Override AG_SYNC_PORT")
def run(host: str | None, port: int | None) -> None:
    """Run the server in the foreground."""
    from .config import get_settings
    from .observability import init_sentry
    from .tls import ensure_cert

    init_sentry()
    settings = get_settings()
    bind_host = host or settings.host
    bind_port = port or settings.port
    cert_path, key_path = ensure_cert()

    print(f"AccessGrid Sync v{__version__}")
    print(f"DB: {settings.db_path}")
    print(f"TLS cert: {cert_path}")
    print("Web UI URLs:")
    print(f"  https://localhost:{bind_port}")
    for ip in _local_ips():
        print(f"  https://{ip}:{bind_port}")
    print()

    uvicorn.run(
        "agsync.server:create_app",
        factory=True,
        host=bind_host,
        port=bind_port,
        log_level=settings.log_level.lower(),
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
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


@cli.command("connect")
@click.argument("uri")
def connect_cmd(uri: str) -> None:
    """Capture a PACS session for the running service.

    The OS invokes this with an agconnect:// URI when the operator clicks
    Connect in the web UI. It opens a throwaway browser at the PACS login
    page, waits for the sign-in, and hands the session back encrypted.
    """
    from .connect import run_from_uri

    _bootstrap_logging()
    raise SystemExit(run_from_uri(uri, on_status=click.echo))


@cli.command("register-uri")
def register_uri_cmd() -> None:
    """Register the agconnect:// handler for the current user.

    Run this as the operator who will do the signing in — registration is
    per-user, so it needs no elevation and touches nothing machine-wide.
    """
    from .connect import register as uri_register

    click.echo(f"Registered agconnect:// -> {uri_register.register()}")


@cli.command("unregister-uri")
def unregister_uri_cmd() -> None:
    """Remove the agconnect:// handler for the current user."""
    from .connect import register as uri_register

    click.echo(uri_register.unregister())


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
