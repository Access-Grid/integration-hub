# ag-sync-tool

Service that synchronizes credentials from a PACS (Physical Access Control System)
into AccessGrid. Runs as a Windows Service on a NUC, exposes a web UI on port 5355.

## Supported PACS

- Avigilon Unity (Plasec) — production
- Avigilon Alta Access (cloud / OpenPath) — production
- CDVI Atrium (on-prem) — production
- Lenel OnGuard — stub (interface only)

### CDVI Atrium

On-prem controllers speaking Atrium's encrypted-XML protocol. Connection
settings: controller URL, Atrium username, Atrium password.

**Enrollment trigger (per card).** A card is synced when the marker
`[accessgrid]` appears in its **Display Name** in the Atrium UI —
optionally with a wallet hint, `[accessgrid-apple]` or
`[accessgrid-android]`. Surrounding label text is fine, e.g.
`Amy iPhone [accessgrid-apple]`. The trigger is per-card, so one of a
user's cards can be enrolled without the others. The pass is provisioned
for the card's assigned user, who must have an **email** set in Atrium
(stored in the SDK `cfg2` record; there is no phone field).

**Card data → AccessGrid.** CDVI stores the credential as a single encoded
hex `number`. The wizard offers a per-vendor choice for how it reaches
AccessGrid:

- **Site code + card number** (default) — the `number` is decoded into an
  8-bit site code (high byte) + 16-bit card number (low two bytes). Assumes
  a 26-bit H10301 layout.
- **Raw `file_data`** — the encoded value is transmitted verbatim
  (left-padded to 16 hex chars). Use this for non-26-bit card formats where
  the decode would be wrong. Site code + card number are still written to the
  AccessGrid card metadata for dedupe and debugging.

**Writeback / deletion.** The sync tool **never creates or modifies CDVI
users**, and never creates, deletes, or unassigns a card. The only write it
performs is toggling an existing card's State (suspend = set inactive,
reactivate = set active). Deleting a triggered card (or its user) in Atrium
removes the corresponding AccessGrid pass on the next cycle.

**Note on Atrium ids.** Atrium reuses object ids — a deleted card's id is
handed to the next card created, and existing cards are never renumbered.
Credentials are therefore tracked by their physical identity (site + card
number), not the mutable slot id, so a deleted-and-recreated card doesn't
churn its AccessGrid pass.

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
