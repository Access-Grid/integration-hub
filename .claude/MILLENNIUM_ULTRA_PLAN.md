# Millennium Ultra — integration plan

Plan for adding **Millennium Ultra** (MGI Access) as a PACS adapter in ag-sync-tool.
Everything below was verified against the live **ICON** tenant at
`hosted8.mgiaccess.com` on 2026-08-18 unless marked otherwise.

Working spike lives in `.claude/millennium-ultra-spike/`.

---

## 1. Scope — settled decisions

| | |
|---|---|
| **Direction** | Read-only, modelled on the CDVI adapter. Millennium Ultra is the source of truth; we never create cardholders or cards. |
| **Surface** | The hosted web interface only. HTML forms + JSON helper endpoints. **No database access** — considered and explicitly ruled out. |
| **Trigger** | A card's **card format**, chosen at config time from the tenant's own list. Per-card, so one of a cardholder's three slots can be enrolled alone. |
| **Writeback** | Only the `Active` checkbox on an existing card. No card or cardholder is ever created, deleted, or unassigned. |
| **Login** | Always through a Chromium we control. The operator signs in and clears the captcha there; we lift the session cookie out of the browser. We never attempt a programmatic login and never store a Millennium Ultra password. |
| **Hosting** | Windows Service on a PC on the customer network, as today. |
| **Vendor id** | `millennium_ultra` |
| **file_data** | Not supported — facility code and card number are already separate decimal fields, so there is nothing to decode. `supports_file_data = False`. |

---

## 2. What the config screen asks for

### Stage 1 — Where

| Field | Kind | Example |
|---|---|---|
| Login URL | url | `https://hosted8.mgiaccess.com` |

That is the whole stage. There is deliberately **no username or password field** — the
operator types those into the login window in stage 2, so no Millennium Ultra password
ever reaches Integration Hub or its database. Company name is read from the
`UltraCompanyName` cookie once the session is captured, and shown read-only.

### Stage 2 — Sign in

| Control | Kind | What it does |
|---|---|---|
| Open login window | button | Opens a Chromium we control on the Millennium Ultra login page. Operator enters company/username/password there, clears the captcha, signs in. |
| Paste a session instead | textarea | Fallback when no window can be opened. Accepts a `Copy as cURL` command, a raw cookie header, or the bare `.AspNet.UltraAuth` value. |

Either route ends with a real request confirming the session and reporting
**"Connected as ICON — 1,842 cardholders found"**. Report the count, not a bare OK: a
valid session and an account that can see the roster are different failures.

### Stage 3 — Sync settings

| Field | Kind | Notes |
|---|---|---|
| Card format | select | Pulled using the Millenium ultra web app. Discovered from the tenant, never hardcoded. Shows how many cardholders already match. |
| Email domain | text | Default `iconcreds.com`. Used to synthesize addresses — no cardholder has one. |

### This needs a two-stage wizard

Today's wizard is single-shot: static `connection_fields` from `PacsDescriptor` → test →
save. Millennium Ultra cannot work that way, because the card-format dropdown can only be
populated after a live session exists to read it from. Stage 3 must render *after*
stages 1–2 succeed. That is a real change to `routes/wizard.py` and is the main reason
this adapter is not a copy of CDVI.

Add an `auth_mode` flag to `PacsDescriptor` (`credentials` vs `browser_session`) so the
wizard renders the right step per vendor.

---

## 3. Verified findings

### The captcha is real and server-side
A login POST with an empty `g-recaptcha-response` and a valid anti-forgery token returns
`302 → /Account/Login` with no auth cookie; the target renders **"Invalid Captcha !"**.
Credentials are never reached, so probing carries no lockout risk.

It is **reCAPTCHA v2 checkbox**, not invisible v3: `recaptcha/api.js` with no `?render=`,
plus `<div class="g-recaptcha" data-sitekey="6LeVCrcU…" data-callback="enableBtn">`.
Cannot be cleared headlessly. The widget is conditionally rendered
(`if ($("div.g-recaptcha").length > 0)`), implying a server-side toggle exists — worth
asking MGI for an exemption, which would make the integration fully unattended.

### The auth cookie is long-lived
A `.AspNet.UltraAuth` cookie roughly 24 hours old still returned `200` from the roster
endpoint. This is what makes an operator-assisted login practical rather than a daily
chore. Whether it slides indefinitely under our polling is **still unknown**.

### Scale: 1,842 cardholders
27 `GetList` calls total ~350 KB. But card data lives only on the per-cardholder page:
**89.7 KB and 0.36–0.49 s each** — ~165 MB and ~12 minutes for a serial full scan.

Letter distribution: S=169, C=167, M=164, B=128, G=124, R=121, L=118, P=105, D=100,
K=91, A=81, F=75, W=57, N=56, T=53, O=51, H=44, V=36, E=31, I=20, J=19, Z=13, Q=9,
Y=6, U=3, X=1, Other=0.

### No cardholder has an email or phone
Across seven sampled records, `EMail`, `Phone` and `CompanyEMail` were empty **and**
rendered `disabled="disabled"` — the tenant has the Personal Information module off.
Phase 1 skips anyone with neither email nor phone, so this would have provisioned zero
passes. Resolved by synthesizing addresses (§5).

### Card fields parse cleanly
Cardholder 10301 slot 1: `CardID`=6630, `EncodedCardNumber`=30373420026,
`FaciltyCode`=0, `CardFormat`=7, `Active` checked. Slots 2 and 3 carry **no `value=`
attribute at all** — that absence is the empty signal.

### Labels are re-purposed per tenant
`MiddleName` is labelled "Card Type" and holds HID/RFID; user fields are labelled Unit
Number, Resident Type, and "test". **Parse by `name=` attribute, never by visible label.**
Card format ids are likewise tenant-specific.

### The roster's `IsActive` is a UI selection flag
`GetList/10301` returns `IsActive: true` for 10301 and `false` for 11211;
`GetList/11211` returns the opposite. Same cardholders, flag follows the id in the
path. It means "currently selected in the list", **not** "cardholder is enabled", and
must not be used for `Person.active`.

### A bulk report exists, but its data is not inline
`GET /UltraReports/View?id=36&c=1&tz=-240` returns every cardholder grouped by card format
in ~3 s, but renders a DevExpress viewer whose rows load over `/DXXRDV.axd` callbacks.
Promising, not free.

---

## 4. Endpoint reference

| Method | Path | Notes |
|---|---|---|
| POST | `/Account/Login` | `CompanyName`, `LoginName`, `Password`, `g-recaptcha-response`, `__RequestVerificationToken`. Success → `302 /` + sets `.AspNet.UltraAuth`, `UltraCompanyName`. Captcha failure → `302 /Account/Login`. |
| GET | `/Cardholders/Cardholders` | Outer frame. |
| GET | `/Cardholders/CardholdersHelper/GetList/{id}?firstLetter={65..90\|0}` | JSON roster for one letter. Fields: `ID`, `IsActive`, `url`, `ParentID`, `Name` (`"Last, TYPE. First"`). Needs `X-Requested-With: XMLHttpRequest`. |
| GET | `/Cardholders/Cardholders/Index/{id}?pw=200` | Cardholder detail HTML, ~90 KB. |
| POST | `/Cardholders/Cardholders/Index/{id}` | `multipart/form-data` save. Success → `302`. A `200` means validation failed; the body carries the reason. |
| GET | `/Cardholders/Cardholders/ValidateEncodedCardNumber?…` | Returns literal `true`/`false`. |
| GET | `/UltraReports/Select/index/1?rid=36` | "Cardholders by" report picker. |
| GET | `/UltraReports/View?id=36&c=1&tz=-240` | Runs it. `ReportTypes.CardholdersBy=36`, group-by Card Format = `1`. |

Page constants worth knowing: `cardsPerCardholder = 3`, `accessLevelsPerCard = 10`,
`accessLevel1FieldID = 200`, `DefaultCardExpirationPeriod = 730`,
`IsEmployeeIDIsRequiredFiled = true`.

### Card slot form fields (N = 1..3)

```
Card_N_CardID              Card_N_EncodedCardNumber   Card_N_BadgeType
Card_N_PrintedCardNumber   Card_N_ExpirationDate      Card_N_CardClass
Card_N_ActivationDate      Card_N_FaciltyCode         Card_N_PinCode
Card_N_Active              Card_N_CardFormat          Card_N_IssueCode
Card_N_EnableCardsExpiration   Card_N_CardExpirationCount
Card_N_Tenant_0            Card_N_AccessLevels
```

Note the vendor's spelling: **`FaciltyCode`**, not FacilityCode.

### Card formats on the ICON tenant

`1` Wiegand Card · `2` Ultra - Wiegand 26 No FC · `3` Marlok · `4` ABA Card ·
`5` None · `6` Blue 37 · `7` HID 37

Existing physical cards use **7 (HID 37)** — see §10.

---

## 5. Mapping to `PacsAdapter`

| Contract field | Source | Notes |
|---|---|---|
| `Person.id` | Cardholder ID | From `GetList` and the form's `ID` field. |
| `Person.full_name` | `FirstName` + `LastName` | Compose as `"First Last"`, not the roster's `"Last, HID. First"`. |
| `Person.email` | synthesized | Lowercased first+last at the configured domain. |
| `Person.active` | *(always True)* | ⚠️ **Not `IsActive`.** That roster flag marks the row currently *selected* in the UI list, not whether the cardholder is enabled — requesting the list with a different id in the path moves the `true` to that row. Real state lives on each card's Active checkbox. |
| `Credential.id` | `facility:encoded` | Physical identity, **not** the slot index or `CardID` — same slot-reuse hazard CDVI guards against. |
| `Credential.site_code` | `Card_N_FaciltyCode` | Decimal. |
| `Credential.card_number` | `Card_N_EncodedCardNumber` | Decimal, up to 64-bit. No decoding. |
| `Credential.trigger_active` | `Card_N_CardFormat` | True when the slot's selected format matches the configured id. |
| `Credential.status` | `Card_N_Active` | Checked → ACTIVE, else SUSPENDED. |
| `activate_date` / `deactivate_date` | `Card_N_ActivationDate` / `Card_N_ExpirationDate` | Format `MM/DD/YYYY hh:mm AM`. **Parse defensively** — the app emits the malformed `00:00 AM` for midnight. |

### Synthesized emails

Pattern `first + last @ <domain>`, lowercased with non-alphanumerics stripped. Both the
pattern and the domain belong in config, not hardcoded.

**Collisions are guaranteed and must be handled.** The roster contains four separate
records named "Acebedo, RFID. Jose", plus duplicated Andersons and Alhadeffs. Append the
cardholder id on collision → `joseacebedo.10411@iconcreds.com`.

⚠️ Passes sent to a domain with no real mailboxes will not reach anyone by email. This
works only if passes are distributed through another channel — confirm before launch.

---

## 6. Session capture — the login window

**Proven end to end by the spike.** Captured a real 448-char HttpOnly
`.AspNet.UltraAuth` from `hosted8.mgiaccess.com`, sealed it, passed it through the
browser, and decrypted it service-side.

### Why nothing simpler works

- **iframe** — the login page ships a frame-buster:
  `if (self !== top) top.window.location.href = self.location.href;`
- **`window.open()` + read the cookie** — foreign origin, and `.AspNet.UltraAuth` is
  HttpOnly. Page JS can never read it.
- **Reverse-proxy the login** — reCAPTCHA v2 site keys are domain-bound. Rendering MGI's
  key on our host produces *"Invalid domain for site key"* and the widget refuses to render.
- **Bundled extension using `chrome.cookies`** — the one in-browser API that *can* read an
  HttpOnly cookie, but **Chrome has removed command-line extension loading**. Tested
  against Chrome 151 with `--load-extension` alone, with
  `--disable-features=DisableLoadExtensionCommandLineSwitch`, and with the
  `--disable-extensions-except` pairing. In all three the extension never loaded —
  neither the service worker nor a content script ran.

So: **CDP is the mechanism.** Remote debugging on the same build works normally.

### Flow

```
--app=<login url>  --user-data-dir=<temp>  --remote-debugging-port=0
--no-first-run  --no-default-browser-check  --window-size=520,760
```

1. `POST /connect/begin` → service mints a `launch_id` and a one-shot AES-256-GCM key.
2. Launch Chromium. Real port lands in `<user-data-dir>/DevToolsActivePort` (line 1),
   which doubles as the readiness signal.
3. `/json/list` → page target's `webSocketDebuggerUrl` → attach, `Network.enable`.
4. Poll `Network.getAllCookies` ~1/s. Appearance of `.AspNet.UltraAuth` **is** the success
   signal — no page parsing, no navigation tracking, nothing injected into the vendor's form.
5. Seal `{.AspNet.UltraAuth, UltraCompanyName}` with the launch key; base64url.
6. `Page.navigate` → `<server>/connect/callback?id=<launch_id>&d=<ciphertext>`.
7. Service decrypts, verifies the launch id, stores into the Fernet settings blob,
   discards the key. Renders "Connected".
8. Close browser, delete temp profile.

**Why encrypt a URL param when the service could just read the cookie itself?** Because
`agsync connect` is a *separate process* from the running service — the browser is
genuinely acting as an untrusted courier between them. The launch id is consumed on first
use, so a replayed URL fails, and a leaked access-log line is ciphertext whose key no
longer exists. Measured payload: 915 chars, comfortably inside any URL limit.

**No Python dependencies added.** `websockets` 16.0 already ships transitively via
`uvicorn[standard]`; `httpx` and `cryptography` are direct dependencies.

### Two constraints on shipping the browser

- **Do not put Chromium inside the exe.** Both releases build `--onefile`, which extracts
  the whole archive to temp on *every* launch. The binary is 21 MB today. **Decision:
  fetch a pinned Chrome for Testing build into `%LOCALAPPDATA%` on first use,
  checksum-verified.** Patching becomes a config bump rather than a rebuild.
- **A Windows Service cannot show a window.** Services run in Session 0, isolated from
  every interactive desktop since Vista — not a permission that can be configured away.
  Two options:
  - `WTSGetActiveConsoleSessionId` → `WTSQueryUserToken` → `CreateProcessAsUser`.
    `pywin32` is already a dependency.
  - **`agsync connect` as a CLI the operator runs on that PC** — runs in their session,
    so the window just appears. Far less code; this is what the spike implements.

  If nobody is logged in at the console, say so and offer the paste fallback rather than
  launching into the void.

Shipping the browser also sidesteps a real hazard: Chrome 114+ honours a
`RemoteDebuggingAllowed` enterprise policy, so on a managed fleet the installed Chrome may
ignore `--remote-debugging-port` entirely.

### TLS

**No certificate-bypass flags** — neither `--ignore-certificate-errors` nor the SPKI
allowlist. The service serves HTTPS on 5355 with a self-signed cert, so the operator
clicks through the interstitial, exactly as they already do to reach the web UI. Chromium
preserves the full URL across it, so ciphertext and launch id arrive intact after
**Advanced → Proceed**.

Warn *before* the button is clicked that a certificate warning is expected — otherwise a
normal step reads as a failure.

### Note on the profile

The spike uses a throwaway profile. A **persistent** one would carry forward the
certificate exception and Google's `_GRECAPTCHA` cookie (observed in the captured jar),
which tends to reduce how often the v2 checkbox escalates to an image challenge. Worth
considering.

---

## 7. Reading 1,842 cardholders per cycle

The engine sets its interval to 3× snapshot time, capped at 600 s. A 12-minute serial
snapshot blows through that and leaves the tool running back-to-back full scans forever.

**Two-tier scan.** Every cycle, refresh the cheap roster to catch added/removed/deactivated
cardholders. Fetch detail pages only for cardholders that are new or already tracked as
enrolled — a handful once warm. Separately, sweep a configurable slice of the full roster
each cycle so everyone is re-checked within a few hours, catching cards newly switched into
the trigger format. At six concurrent connections a cold full scan lands in ~2 minutes.

**Make the interval ceiling per-adapter.** `MAX_INTERVAL_S = 600` is tuned for fast PACS
APIs; move the cap onto the descriptor so CDVI and Alta keep their cadence.

**The bulk report is a spike, not a dependency.** If the DevExpress export can be driven
directly, discovery collapses from 1,842 requests to one. Attempt it *after* the two-tier
scan works.

---

## 8. Two-way status and expiry

### Expiration moved in Millennium Ultra → update AccessGrid
Phase 1 stamps `expiration_date` once at provision time and nothing revisits it. Phase 6
compares person fields only and never looks at the credential. So:
- Add a `last_synced_expiration` column to `ag_credentials`, with a migration.
- Resolve the credential alongside the person in phase 6.
- Diff both `deactivate_date` and `activate_date`; on drift call
  `ag.access_cards.update(expiration_date=…)`.

This benefits every adapter — put it in the shared phase, not vendor code.

### Suspended or revoked in AccessGrid → uncheck Active
Phase 4 already walks AG-side changes and calls `update_credential_status`, so the adapter
mainly implements that method. **Gap to close:** `_AG_TO_CRED_STATUS` covers only
`active`, `created`, `suspended` — a revoked or deleted card falls through and does
nothing. Map those to `SUSPENDED`.

### ⚠️ The write is the riskiest code here
Writeback is a read-modify-write: GET the page, take a fresh `__RequestVerificationToken`,
flip only `Card_N_Active`, re-post **every other field unchanged** as multipart.

A missed field silently blanks real data in a production access-control system. Two
safeguards are non-negotiable:
1. Serialize the form **generically from the parsed HTML**, not from a hand-written field
   list, so unknown fields survive automatically.
2. Gate the first write behind a **dry-run** that logs the diff without posting.

Test on cardholder **11587** ("Access Grid"), which already exists as a scratch record.

Note: several inputs are marked `disabled` in the HTML yet appear in the captured POST —
the page's JavaScript re-enables them before submit. Mirror the captured payload, not the
raw HTML's disabled flags.

### When the session dies
The web session is the only way in, so a dead cookie stops everything. Detect
`302 → /Account/Login` centrally in the client, distinct from a network error — one needs
a human, the other recovers by itself. Then pause the engine, set
`pacs_reachable = false`, log an error-level entry, and show a **Reconnect required**
banner. Store `connected_at` and show session age so reconnects can be scheduled rather
than discovered.

---

## 9. Build order

1. **Parsers** — `millennium_ultra/parse.py`. Pure functions over saved HTML: card slots,
   card formats, roster, synthesized emails with collision handling. No network, no
   session. Fixtures already captured.
2. **Client** — `millennium_ultra/client.py`. Session-cookie auth, pasted-session
   ingestion, expiry detection, `test_connection` returning the cardholder count.
3. **Adapter + descriptor** — `list_people`, `list_credentials`, format trigger, two-tier
   scan. Register the vendor, bilingual `HELP_TEXT`, i18n wiring, and extend the CI
   vendor-set assertion in `.github/workflows/ci.yml`.
4. **Login window** — `lib/chromium.py` (pinned download, checksum, cache) +
   `lib/browser_login.py` (launch, CDP capture, sealed redirect, teardown) + the
   `agsync connect` subcommand. Promote from the spike.
5. **Wizard** — two-stage flow, format discovery + match count, paste fallback, reconnect
   banner, `/connect/begin` and `/connect/callback` routes.
6. **Expiry drift** — phase 6 + tracking column + migration.
7. **Status writeback** — dry-run first, plus the phase-4 revoked/deleted mapping.

Items 1–5 are independently useful: together they deliver a read-only integration that
provisions passes, which is the bulk of the value.

Keep the browser-login code in `lib/`, not under `millennium_ultra/` — Millennium Ultra is
the first captcha-walled portal we've hit, it will not be the last.

---

## 10. Open questions

**Which card format means "enrolled"?** Existing cards on this tenant use HID 37, so
selecting it would enroll most of the building — well over a thousand passes, silently.
The clean setup is a dedicated format created for AccessGrid so the trigger starts at zero
matches. Does MGI need to create one? The stage-3 match count makes the blast radius
visible, but does not remove the need to ask.

**Will passes really be delivered to `@iconcreds.com`?** Those mailboxes do not exist, so
email delivery is a no-op. Fine if distribution happens another way, but it changes what
"provisioned" means operationally.

**How long does the session actually last?** Verified at >24 h. Whether it slides
indefinitely under our polling, or has a fixed ceiling, sets the operator's reconnect
burden. Cheap to answer by leaving a session idle and re-testing.

**Can MGI disable the captcha for an integration account?** The widget is conditionally
rendered, so a server-side flag almost certainly controls it. If granted, §6 becomes dead
code and the integration is fully unattended.

---

## 11. Spike

`.claude/millennium-ultra-spike/`

| File | What it does |
|---|---|
| `connect_spike.py` | `agsync connect` proof: finds a browser, launches `--app` into a temp profile, attaches over CDP, polls for the auth cookie, seals it, redirects to the callback, cleans up. |
| `mock_server.py` | Stands in for the running service: `POST /connect/begin` mints the launch key, `GET /connect/callback` decrypts, verifies, and renders the success page. |

Run:

```bash
python mock_server.py &
python connect_spike.py 'https://hosted8.mgiaccess.com/Account/LogIn' '.AspNet.UltraAuth' 'http://127.0.0.1:8921'
```

Observed output — a real capture, end to end:

```
CAPTURED  : .AspNet.UltraAuth (448 chars, httpOnly=True)
  domain  : hosted8.mgiaccess.com
  jar     : 6 cookies -> .AspNet.UltraAuth, DisableAlarmSound, UltraCompanyName,
                         _GRECAPTCHA, __RequestVerificationToken, timeoffset
sealed    : 915 chars of ciphertext in the URL
[server] DECRYPTED and stored 2 cookie(s):
[server]   UltraCompanyName  = ICON
[server]   .AspNet.UltraAuth = H9xuRnW51VrY-Q0V_1bMP5z7FaprIG86... (448 chars)
[server] key discarded — a replay of this URL now fails
```
