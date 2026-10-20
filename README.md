# ag-sync-tool

Service that synchronizes credentials from a PACS (Physical Access Control System)
into AccessGrid. Runs as a Windows Service on a NUC, exposes a web UI on port 5355.

## Supported PACS

- Avigilon Unity (Plasec) — production
- Avigilon Alta Access (cloud / OpenPath) — production
- CDVI Atrium (on-prem) — production
- Millennium Ultra (MGI) — production
- Lenel OnGuard — interface only

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
register the handler (per-user; no elevation). On Windows that writes an
`HKCU` protocol handler. macOS delivers a URL as an Apple Event rather than
an argument, so there it compiles a small AppleScript applet into
`~/Applications/AG Connect.app` — a bundle wrapping a plain executable would
be launched with no arguments and the URL would be lost.

Sessions expire, roughly hourly, and they do not say so. A dead cookie has
been seen producing three different shapes, none of which looks like an
error: a redirect to the login page, an empty 200 body, and a valid empty
`[]` alongside HTTP 500 on the cardholder screens. The last is the dangerous
one — a well-formed empty answer is indistinguishable from a PACS with no
cardholders, and reads downstream as "everyone was deleted". All three raise
`PacsAuthExpired`, so syncing pauses, a banner appears, and — if SMTP is
configured under Settings → Notifications — a message asks someone to
reconnect. The banner appears on every page, not just the status screen, and
is itself the link: it points straight at the `agconnect://` side-car, so
re-authenticating is one click from wherever the operator happens to be. A
PACS that does not answer at all gets a different banner, since
re-authenticating would not help. An install that genuinely has zero cardholders would be
misreported by the empty-roster rule; that is the intended trade, since
being wrong that way prompts a human and being wrong the other way stops the
sync in silence.

**Enrollment trigger (card format).** At setup you choose one card format,
read live from the install; both its id and its display name are stored,
and the name is what the export is matched against. Any cardholder holding a
card in that format is enrolled. An *empty* card slot cannot carry a
format, so the trigger is always a slot holding a real card. It can be
changed later under Settings → Enrollment trigger. A card whose format is
changed away from the trigger is not revoked: its pass is held, untouched,
until the format comes back.

**Reading: one export per cycle.** Each cycle reads Millennium's bulk
cardholder export — a queued job, three requests, one row per cardholder
with all three card slots and their access levels — and nothing else.
Cardholders are keyed by **Employee ID**, since the export has no record
id; a row without one, or sharing one with another row, is skipped with an
ERROR. An empty export, or a trigger with no stored name, fails the cycle
rather than reading as nobody enrolled. A cardholder with no valid email
address gets `first.last.<Employee ID>@<configured domain>`.

**Two directions**, read from the AccessGrid card template's `protocol`
rather than asked for, and re-read every cycle along with whether the
template is a pair. An unreadable template falls back to DESFire, the
direction that never writes a card.

- **DESFire** — the card already exists in Millennium; its facility code and
  card number are copied out to AccessGrid. The only thing changed in
  Millennium is the card's Active box.
- **HID Seos** — AccessGrid mints the credential. The trigger card marks
  *who* gets a pass; AccessGrid allocates the facility code and card number,
  and the first card is written over the trigger card. A pair's second card
  goes into an empty slot, so a pair needs one free slot. Each card written
  takes the trigger format, the trigger card's access levels (all ten), an
  activation date of now, and Active off until the pass is installed.

  A pair is worth calling out: issuing against one returns a *unified pass*
  rather than a card. Its credentials are in `details`, one per platform,
  each with its own card number, and the pass itself has none — so both
  halves are read from there.

  Finding that pass again needs care, because listing the template returns
  the individual cards and *not* the unified id. Rather than depend on which
  id comes back, every issue mints a `sync_ref`, stamps it into the card
  metadata before the call, and records it in `ag_credentials`. Each card an
  issue produced carries it, so a pair resolves to both halves. Lookups try
  the reference first, then the id, then employee id +
  `pacs_credential_id` for cards issued before references existed — a weak
  key, since it repeats if a credential was ever issued twice. See
  `Snapshot.resolve_ag_cards`.

**Which trigger cards get a pass.** `pacs_cards` records every card a pass
rests on, one row per card, with the card number and facility code
encrypted. Each trigger-format slot is checked against it:

- a recorded card is its existing pass, followed if it moved slot;
- a recorded slot now holding a different card, a card recorded on another
  cardholder, or a tracked pass with nothing recorded is logged as an ERROR
  and mailed to the notification address — nothing is issued, and the
  existing pass is held;
- anything else is a new pass, recorded at once as `card` (then `card-2`,
  ...), which phase 1 issues and keeps retrying until it has.

A held pass (`Credential.hold`) is left exactly as it is by every phase.

**Writing: one import per cycle.** The phases only record what should
change, as pending rows in `pacs_cards`. Once they have all run,
`finish_cycle` sends one Millennium cardholder import — "update matched
records only", matched by Employee ID, carrying only the columns being
changed — and deletes any cards marked for deletion (found by an Employee ID
search, then that cardholder's page). It keeps what the import log confirms;
a row the log skipped stays pending for the next cycle. Dry run sends
nothing.

- Suspend and resume switch the card's Active box. A card we switched on
  that an operator has since switched off is not switched back on.
- A Seos card AccessGrid deletes, or its holder unlinks, is deleted from
  Millennium. DESFire cards are the customer's and are never deleted.

**Revocation.** A card gone from Millennium revokes its pass through phase
3. A read with under 30% of the people of the last complete read revokes
nobody for being missing.

**Seeing what reached the PACS.** For Seos the AccessGrid pass existing is
only half the story — the pass opens doors when its card numbers are in
Millennium, and not before. The Credentials page reports that per row ("2
card numbers saved to Millennium Ultra"), with the numbers themselves on the
badge for looking up a pass that is not working. Read from `pacs_cards`.

**Changing a person's details.** Phase 6 pushes name, phone, title and the
card's expiration date to AccessGrid, as metadata updates. It does **not**
push email: AccessGrid rejects it on an issued pass, since the address is
what the pass was delivered to. A changed address is logged once. A pass's
expiry is recorded the first time it is seen, rather than pushed, so
existing passes are not all rewritten at once.

**Pass details.** A pass carries the card's own validity from Millennium as
its start and expiration dates, rather than "whenever the sync noticed it"
— the stored text is local to the install, so it is converted using the
`timeoffset` captured at sign-in. Title and classification describe the
deployment rather than the person, so they are set once under Settings →
Pass details; a PACS that does carry a per-person title still wins.

**Upgrading an existing install.** On its first cycle the adapter moves
everything keyed by Millennium record id to Employee ID: it reads each
tracked cardholder's page once and re-keys tracking and `pacs_cards` in one
transaction. A record it cannot read, with no Employee ID, or sharing one,
stops the move — fix it in Millennium — and nothing syncs until it
completes. It also fills in the trigger format's name, migration 011 moves
the old ledger blob into `pacs_cards`, and DESFire passes tracked by slot
(`slot2`) are adopted under their existing ids.

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
