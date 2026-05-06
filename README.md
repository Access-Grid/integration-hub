# ag-sync-tool

Service that synchronizes credentials from a PACS (Physical Access Control System)
into AccessGrid. Runs as a Windows Service on a NUC, exposes a web UI on port 5355.

## Supported PACS

- Avigilon (Plasec) — production
- Lenel OnGuard — stub (interface only)

## Quick start (development)

Requires Python 3.12 on Windows 11.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]

$env:AG_SYNC_ENCRYPTION_KEY = python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
agsync run
```

Open `https://<your-ip>:5355` and run the setup wizard. The wizard
requires the bootstrap license key (hardcoded in `routes/wizard.py`)
to create the first admin account. The first launch generates a
self-signed TLS cert under `<db dir>/tls/`; browsers will warn once
and you click through.

## CLI commands

```
agsync run                # Run the server in the foreground
agsync install-service    # Install as a Windows Service (requires admin)
agsync uninstall-service  # Remove the Windows Service
agsync reset-admin        # Wipe the admin user — wizard will re-run on next start
```

## Configuration

`AG_SYNC_ENCRYPTION_KEY` is the only required environment variable. It encrypts
secrets at rest in the SQLite database. Lose it and you must re-run the wizard.

The SQLite database lives at `%LOCALAPPDATA%\AGSyncTool\app.db`.

## Architecture

- `src/agsync/server.py` — FastAPI app
- `src/agsync/sync/` — sync engine and phases
- `src/agsync/lib/pacs/` — PACS adapters (one subpackage per vendor)
- `src/agsync/ag/` — AccessGrid HTTP client
- `src/agsync/templates/` — Jinja2 templates (HTMX-driven)
- `src/agsync/locales/` — i18n dictionaries

See `docs/sync-phases.md` (in the sibling `avigilon-unity-chrome-plugin` repo) for
plain-English description of the sync phases.
