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
register the handler (per-user; no elevation). On Windows that writes an
`HKCU` protocol handler. macOS delivers a URL as an Apple Event rather than
an argument, so there it compiles a small AppleScript applet into
`~/Applications/AG Connect.app` — a bundle wrapping a plain executable would
be launched with no arguments and the URL would be lost.

Sessions expire, roughly hourly, and they do not say so. A dead cookie has
been seen producing three different shapes, none of which looks like an
error: a redirect to the login page, an empty 200 body, and a valid empty
`[]` alongside HTTP 500 on the cardholder screens. The last is the dangerous
one — a well-formed empty roster is indistinguishable from a PACS with no
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
read live from the install. Any cardholder holding a card in that format is
enrolled. There is no sentinel field to type. An *empty* card slot cannot
carry a format — Millennium discards one posted for a slot with no card — so
the trigger is always a slot holding a real card. It can be changed later
under Settings → Enrollment trigger; the format list is re-read from the
PACS each time, so one added or renamed there shows up without re-running
setup. Narrowing the trigger un-enrolls everyone holding only the old
format, and phase 2 terminates their passes on the next cycle.

**Two directions**, read from the AccessGrid card template's `protocol`
rather than asked for. The operator is never in a position to make the two
systems disagree, and the engine re-reads it every cycle, so swapping the
template for one of a different technology takes effect on its own. An
unreadable template falls back to DESFire, the direction that never writes.

- **DESFire** — the card already exists in Millennium; its facility code and
  card number are copied out to AccessGrid. Millennium is the source of
  truth and no card is ever written.
- **HID Seos** — AccessGrid mints the credential. The trigger card marks
  *who* gets a pass; AccessGrid allocates the facility code and card number
  (it allocates whenever they are omitted) and the sync engine writes them
  into the cardholder's empty card slots. A cardholder needs **two** free
  slots before Seos will provision, because a pass routinely carries two
  credentials — a card template *pair* issues one per platform, and a holder
  who installs on both a phone and a watch needs one slot each. Running out
  halfway would strand the second.

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

**Write safety.** Millennium's save replaces the entire cardholder record:
any field not echoed back is wiped. Rather than maintain a field list, the
adapter parses *every* control the page served and replays all of them,
mutating only the target slot — reproducing browser submit semantics exactly
(document order, unchecked boxes omitted, empty file parts for the photo and
signature). `tests/test_millennium_html_form.py` asserts that our POST is
byte-identical to a captured browser POST for the same page. Access levels,
photos, user fields and vehicle details are replayed untouched; this adapter
never edits a cardholder's access levels.

**Pass details.** A pass carries the card's own validity from Millennium as
its start and expiration dates, rather than "whenever the sync noticed it"
— the stored text is local to the install, so it is converted using the
`timeoffset` captured at sign-in. Title and classification describe the
deployment rather than the person, so they are set once under Settings →
Pass details; a PACS that does carry a per-person title still wins.

**Writeback.** Suspend and resume toggle the card's `Active` checkbox; the
card keeps its number, dates and access levels. In Seos mode the slots
holding AccessGrid-allocated cards are recorded on our side rather than
stamped into a customer-visible field.

A Seos credential is written into Millennium **once**. Phase 4 re-offers
each tracked card's identities every cycle so a second device is picked up,
but an identity the ledger says we already wrote is never written again: if
it is gone from the cardholder, somebody removed it in Millennium, and that
decision stands. Nothing in any phase creates a credential in the PACS to
match one that exists only in AccessGrid — the only card this integration
creates is the first write for a newly-issued pass.

That removal is a revocation, and it travels back out through the ordinary
route: the credential reports itself suspended, and phase 2 suspends the
AccessGrid pass off the `ag_credentials` tracking table on the next cycle.
Written cards are matched by facility code + card number, not by slot, so
moving one between slots is not mistaken for a deletion and a *different*
card appearing in the old slot is not mistaken for ours. Every written
credential must be present and active for the pass to stay active:
AccessGrid can suspend a pass but not one device on it, so a holder whose
watch credential was deleted cannot be half-revoked, and access control
fails closed.

**Cardholders.** This install stores no email addresses or phone numbers, so
an address is synthesized per cardholder as
`first.last.<record id>@<configured domain>`. The record id is part of it
because the same human appears once per card technology, and each is a
separate pass. The domain is editable under Settings → Synthesized email
addresses; changing it changes every address, and phase 6 pushes the new
ones to AccessGrid on the next cycle, including for passes already issued.
Hand passes out with the QR code on the Credentials page.
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
