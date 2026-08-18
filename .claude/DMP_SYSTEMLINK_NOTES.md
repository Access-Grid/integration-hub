# DMP System Link — reverse-engineering notes

Everything learned while adding **DMP Secure Link** support to ag-sync-tool, from
a customer database dump + the System Link manual + a process memory dump.
Companion tooling lives in `scripts/dmp/`.

Source material used:
- `~/Downloads/Db/` — customer DBISAM database dump (panel **account 9865**)
- `~/Downloads/LT-0570.pdf` — DMP **System Link** manual (2022)
- `~/Downloads/link.DMP` — memory dump of the running System Link process
- `~/Downloads/link_export.xml` — a System Link "Export Accounts" file (key `abc123`)

---

## 1. What System Link is

DMP (Digital Monitoring Products) **System Link** is the Windows desktop app that
programs DMP alarm/access panels. It stores its data in a **DBISAM v4** embedded
database (Elevate Software). Access control runs on DMP **734** modules
(single-door). The process is named `link` (32-bit). There is **no API or SDK** —
it's a GUI app. The receiver in front of the panels is an **SCS-1R/150**.

DBISAM signature (bytes 8–23, identical in every table): `06 8a be 8e 59 23 64 cb
40 3d 71 d2 e3 bc 64 d0`. Field descriptors start at file offset **0x200**,
768 bytes each; basic header (0x00–0x1FF) is plaintext.

---

## 2. Encryption — SOLVED

### DBISAM tables (`.dat` files)
- **80 of 84 tables are encrypted.** Only 4 are cleartext: `DebugTable` and the
  trivial `GPRSRatePlanStrings` / `GPRSStatusStrings` / `GPRSTextPlanStrings`.
- Scheme: **only the 0x200-onward region is encrypted** (basic header stays
  clear, so `total_rows`@0x29, `row_size`@0x2D, `total_fields`@0x2F parse in the
  clear). Cipher = **Blowfish, key = MD5(password) (full 16 bytes), big-endian,
  CBC, IV = 0.**
- **Table password: `DmPdAtAbAsE`** — a DMP compiled-in secret, the same for the
  whole DB. It is NOT the Link Server login (`admin`/`LinkAdmin`), the account id
  (9865), or any dictionary word. Recovering it required the binary/memory.
- Crib to recognize a correct decrypt: field descriptor 1 decrypts to
  `01 00 <namelen> <ASCII field name>` (e.g. `01 00 04 "TIME"` in DebugTable).

### How the password was recovered
1. `scripts/dmp/find_key_in_binary.py` extracts every ASCII + UTF-16LE string
   from `SystemLink.exe` or a **process memory dump**, and tests each against the
   crib. (Windows: Task Manager → right-click `link (32 bit)` with the DB open →
   Create dump file → `link.DMP`.)
2. Password appeared in the dump's string pool. ~720k candidate strings, instant
   hit: `DmPdAtAbAsE`, key_from=md5, keylen=16, big-endian, CBC.

### XML "Export Accounts" file (cleanest production path)
System Link `File > Import and Export > Export Accounts` writes an encrypted
`.xml`, keyed by a password the **operator chooses at export time**.
- File is ASCII: one long UPPERCASE hex string = ciphertext.
- **AES-128, key = MD5(export password), ECB**, PKCS7-ish tail padding.
- Decrypts to `<Panels><Panel>…<Users>…</Users>…</Panel></Panels>`.
- Field values are typed: `DataType="1"` = base64 string, `"3"` = integer,
  `"11"` = timestamp.
- **Because the key is operator-chosen, this needs no DMP secret** — the operator
  runs the export with a key we agree on and we decrypt it. Preferred path.

---

## 3. Data model (confirmed against customer ground truth)

Validated points: **USER_NUM 67 = TYLER SNYDER, CODE `13777`** and **USER_NUM 28 =
SHAWN KANE, CODE `11999`, PROFILE1 `2`** — matched by both the DBISAM decrypt and
the XML export.

### `Users` table (53 cols) — the enrollment source
| Field | Meaning |
|---|---|
| `USER_NUM` | user number / slot (**recycled** over time — same slot reassigned to different people; track by physical card, not slot) |
| `NAME` | display name, **truncated to 16 chars** |
| `FIRST_NAME`/`LAST_NAME` | usually empty in this DB |
| **`CODE`** | **the card number** (string, e.g. "13777"). This is what AccessGrid needs. |
| `CODE_2`, `USER_PIN` | secondary code / keypad PIN (empty here) |
| **`PROFILE1`–`PROFILE4`** | **access-level (profile) assignments** |
| `PROF_NUM` | legacy single-profile (0 when PROFILE1-4 used) |
| `CARD_INFO` | BLOB subtype (DBISAM type 5635) — raw card data |
| `DEPARTMENT`, `ID_NUMBER` | org fields |
| `U_FIELD1`–`U_FIELD3` | custom fields — **all empty in this DB** |
| `ACTIVE` | active flag |
| plus ~30 permission booleans (DISARM, DOOR_ACCSS, …) |

### `Profiles` table (17 rows, 56 cols) = access levels
`PROF_NUM`, `NAME`, **`ACC_AREAS`** (access areas), `SCHED_1..8`, permission flags.

### Other relevant tables
- `Device734` (35 rows) — door/reader config (door names)
- `AreaInfo` (32 rows) — areas
- `Account` (1 row) — panel identity (account 9865)
- `EmailOptions` (1 row) — email-notification config
- `CustomFields` (8 rows) — custom-field definitions (none repurposed for email)
- `Keyfobs` — empty in this DB
- `AccessCode` — the panel keypad **lockout** code, NOT per-user credentials

### DebugTable (cleartext, rolling buffer)
70 MB, ~1,580 live + ~236k deleted rows. Columns: `TIME`, `MESSAGE`, `SOURCE`,
`ID`. Logs raw panel protocol traffic; the `P=` records' credential blob is an
**obfuscated transport format** and does NOT statically decode to the card number
(don't use it — use decrypted `Users.CODE`). Useful only for early recon.

---

## 4. Key implications for the AccessGrid integration

- **Card number = `Users.CODE`.** DMP stores only the extracted card number, not
  the facility/site code (default reader Wiegand: length 26, user-code position
  9, length 17 → keeps the 17-bit card #, discards the 8-bit facility code). Site
  code, if needed, lives in the **Card Format** config per-734, not per-user.
- **No email anywhere in the DB** (Users, custom fields, U_FIELD1-3 all empty).
  AccessGrid provisioning needs an **external email source** — worse than CDVI,
  which at least had the SDK cfg2 record.
- **Enrollment trigger** should key off **Profile membership** (a dedicated
  profile = "sync to AccessGrid"), because names truncate at 16 chars and there's
  no marker field to abuse (unlike CDVI's `[accessgrid]` display-name trick).
- **User-number slots recycle** — dedupe/track by card number, not USER_NUM.
- **No DMP API/SDK.** Real-time option is the panel **Integrator Path**
  (6.5.9.2): TCP stream (ports 2001/2011, optional passphrase) of Door Access /
  User Cmd events — good for ongoing sync, not bulk load.

### Recommended extraction path
1. **Operator runs `Export Accounts`** with an agreed key →
   `decrypt_xml_export.py` parses it. No DMP secret, fully supported.
2. Fallback: **direct DBISAM decrypt** with `dmp_decrypt.py` (password
   `DmPdAtAbAsE`), pure Python, works on a raw file dump.
3. Windows-only alt: `export_via_odbc.py` via the DBISAM ODBC driver.

### Panel connection security (for a live-connection route)
Connecting System Link to a panel uses a **Remote Key** (set once in the panel,
matched in System Link — can be blank) plus **Service Receiver Auth = YES** /
Allow Network Remote in the panel's REMOTE OPTIONS. There is no per-connection
code entered at the keypad.

---

## 5. Tooling (`scripts/dmp/`)

| Script | Purpose |
|---|---|
| `dmp_decrypt.py` | Decrypt + read any DBISAM table to CSV (`--all` dumps everything). |
| `decrypt_xml_export.py` | Decrypt + parse the operator-keyed XML export. |
| `find_key_in_binary.py` | Recover the DBISAM password from exe / memory dump. |
| `export_via_odbc.py` | DBISAM ODBC dump (Windows). |
| `read_debugtable.py` | Early recon from cleartext DebugTable (superseded). |
| `decrypt_table.py` | Original decrypt scaffold (superseded). |

Depends on `pydbisam` + `cryptography` (dev venv). pydbisam quirks handled in
`dmp_decrypt.py`: BLOB subtype 5635 → BLOB; per-row delete flag is unreliable on
these v4 files (live rows carry a nonzero header byte) so read all slots + filter.

Full decrypted DB export: `~/Downloads/dmp_export/` (one CSV per table).

---

## 6. Write / flush to panel (store-and-forward)

Editing the DBISAM DB does **NOT** auto-push to the panel. System Link is
store-and-forward; the DB is a staging copy.

- Editing a user/profile writes the DBISAM record and stamps **`LAST_CHANGE`**.
- Flush is a **manual `Panel > Send`** (§6.5.27): connect to panel → `Panel > Send`
  with options *Changes Only* (send only `LAST_CHANGE > LAST_PNL_CMP` records),
  clear-before-send, Request Events, Update Time, Disconnect on Completion.
- After a send/compare, **`LAST_PNL_CMP`** is stamped. So "needs sending" =
  `LAST_CHANGE` vs `LAST_PNL_CMP` per record.
- Supporting tables: `UserCodesSend`/`UserCodesSendCandidates` (send queue, keyed
  by account), `PanelComparisons` (compare DB↔panel: USERS/SCHEDULES/PROFILES),
  `AutoSendStatus` (batch-send status for the **Account Groups module** §9.6,
  which pushes to all panels in a group — still operator-triggered).
- Per-user `SND_TO_LKS` ("Send To Locks") = include this user when pushing to
  wireless lock devices.
- Reverse direction: **"Send Local Changes"** (Remote Options) lets the *panel*
  push keypad-made changes back to the workstation (None/NET/DD).
- **No continuous DB→panel daemon.** (The only "auto send" is *Auto Send Traps*,
  which is receiver traps, unrelated.)

Implication: the DBISAM DB / XML export = System Link's view. Normally in sync
(recent `LAST_PNL_CMP`) but can be **ahead** (unsent edits) or **behind**
(unretrieved keypad changes). Fine as a read source of truth for credential sync;
but **writing DBISAM alone will not change the panel** — a `Panel > Send` is
required, and there is no API for it (GUI automation or direct panel protocol).
