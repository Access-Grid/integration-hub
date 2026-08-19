# ag-sync-tool

Service that synchronizes credentials from a PACS (Physical Access Control System)
into AccessGrid. Runs as a Windows Service on a NUC, exposes a web UI on port 5355.

## Supported PACS

- Avigilon Unity (Plasec) — production
- Avigilon Alta Access (cloud / OpenPath) — production
- CDVI Atrium (on-prem) — production
- Millennium Ultra (MGI) — production
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

### Millennium Ultra (MGI)

Millennium Ultra has no API, so this adapter drives the same web screens an
operator uses. Connection settings: the Millennium URL, a domain for
synthesized email addresses, and a notification address.

**Signing in (AG Connect).** Millennium's login is protected by a captcha,
so no headless login is possible — a person has to sign in, and the service
borrows the session they get. The service runs in session 0 and cannot put a
window on the operator's desktop, so the browser starts the conversation:
the setup page offers an `agconnect://` link, the OS hands it to the AG
Connect side-car, and the side-car opens a throwaway Chromium at the login
page, waits for the sign-in, and returns the session cookie sealed with a
one-shot AES-256-GCM key held only by the service. Run
`agsync register-uri` once, as the operator who will be signing in, to
register the handler (per-user; no elevation).

Sessions expire. When one does, syncing pauses, a banner appears in the web
UI, and — if SMTP is configured under Settings → Notifications — a message
goes to the notification address asking someone to reconnect. An expired
session is never mistaken for an empty cardholder list.

**Enrollment trigger (card format).** At setup you choose one card format,
read live from the install. Any cardholder holding a card in that format is
enrolled. There is no sentinel field to type. An *empty* card slot cannot
carry a format — Millennium discards one posted for a slot with no card — so
the trigger is always a slot holding a real card.

**Two directions**, chosen at setup:

- **DESFire** — the card already exists in Millennium; its facility code and
  card number are copied out to AccessGrid. Millennium is the source of
  truth and no card is ever written.
- **HID Seos** — AccessGrid mints the credential. The trigger card marks
  *who* gets a pass; AccessGrid allocates the facility code and card number
  (it allocates whenever they are omitted) and the sync engine writes them
  into the cardholder's empty card slots. A cardholder needs **two** free
  slots before Seos will provision, because a holder who installs on both a
  phone and a watch needs one slot each — running out halfway would strand
  the second device.

**Write safety.** Millennium's save replaces the entire cardholder record:
any field not echoed back is wiped. Rather than maintain a field list, the
adapter parses *every* control the page served and replays all of them,
mutating only the target slot — reproducing browser submit semantics exactly
(document order, unchecked boxes omitted, empty file parts for the photo and
signature). `tests/test_millennium_html_form.py` asserts that our POST is
byte-identical to a captured browser POST for the same page. Access levels,
photos, user fields and vehicle details are replayed untouched; this adapter
never edits a cardholder's access levels.

**Writeback.** Suspend and resume toggle the card's `Active` checkbox; the
card keeps its number, dates and access levels. In Seos mode the slots
holding AccessGrid-allocated cards are recorded on our side rather than
stamped into a customer-visible field.

**Cardholders.** This install stores no email addresses or phone numbers, so
an address is synthesized per cardholder as
`first.last.<record id>@<configured domain>`. The record id is part of it
because the same human appears once per card technology, and each is a
separate pass. Hand passes out with the QR code on the Credentials page.
Names come from the roster (`Last, Middle. First`, where this install
repurposes the middle-name field as a card-type label) and are replaced by
the authoritative first/last once a detail page is read.

**Scale.** ~1,800 cardholders, one ~90 KB detail page each. The first cycle
reads all of them to build a profile cache (~5 minutes); after that each
cycle refreshes the enrolled cardholders plus a rotating slice of the rest,
so a newly-enrolled card is still discovered without re-reading 158 MB every
cycle. The roster sweep itself is 26 cheap JSON calls, about two seconds.

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
agsync register-uri       # Register the agconnect:// handler for the current user
agsync unregister-uri     # Remove it
agsync connect <uri>      # AG Connect side-car — normally started by the OS
                          # when the operator clicks Connect in the web UI
```

## Configuration

`AG_SYNC_ENCRYPTION_KEY` is the only required environment variable. It encrypts
secrets at rest in the SQLite database. Lose it and you must re-run the wizard.

The SQLite database lives at `%LOCALAPPDATA%\AGSyncTool\app.db`.

## Architecture

- `src/agsync/server.py` — FastAPI app
- `src/agsync/sync/` — sync engine and phases
- `src/agsync/lib/pacs/` — PACS adapters (one subpackage per vendor)
- `src/agsync/connect/` — AG Connect side-car (captcha-gated PACS logins)
- `src/agsync/ag/` — AccessGrid HTTP client
- `src/agsync/templates/` — Jinja2 templates (HTMX-driven)
- `src/agsync/locales/` — i18n dictionaries

See `docs/sync-phases.md` (in the sibling `avigilon-unity-chrome-plugin` repo) for
plain-English description of the sync phases.
