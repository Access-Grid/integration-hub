# DMP Secure Link — reconnaissance tooling

Scratch tooling for the DMP (Digital Monitoring Products) **System Link**
integration work. DMP access control stores its data in **DBISAM** tables (the
`Db/` customer dump: `*.dat` data, `*.idx` index, `*.blb` blob, `dbisam.lck`).
This directory reverse-engineers that dump so we can design the AccessGrid sync.

Not product code — analysis/recon that we keep because we'll re-run it against
future customer dumps. Depends only on `pydbisam` (dev venv).

## Scripts

| Script | What it does |
|---|---|
| `dmp_decrypt.py` | **SOLVED.** Decrypts + reads any DBISAM table to CSV, in pure Python. Password `<the DMP key>`; Blowfish/MD5 key/CBC/IV=0 from 0x200. `--all` dumps the whole DB. |
| `decrypt_xml_export.py` | Decrypts a System Link **Export Accounts** `.xml` (AES-128-ECB, key=MD5(operator password)). Cleanest production path — key is operator-chosen, no DMP secret. |
| `find_key_in_binary.py` | Recovers the DBISAM password from `SystemLink.exe`/a memory dump (string extract + crib test). This is how `<the DMP key>` was found. |
| `export_via_odbc.py` | Alternative: dump tables via the DBISAM ODBC driver (Windows-only). |
| `read_debugtable.py` | Early recon: roster from the one cleartext table, `DebugTable.dat` (no key needed). |

Two working extraction paths, both validated (JANE DOE→CODE 10001,
JOHN SMITH→CODE 10002/PROFILE1 2): decrypt the DBISAM `.dat` files directly, or
decrypt the operator-keyed XML export. **Users.CODE = card number,
PROFILE1-4 = access levels. No email anywhere in the DB.**

```bash
# On the customer's System Link / Link Server machine (Windows):
python scripts/dmp/export_via_odbc.py --dsn DMP_SYSTEMLINK_READONLY

# Offline, from a raw dump (needs the DMP key):
python scripts/dmp/dmp_decrypt.py /path/to/Db --password KEY --all --outdir export/
python scripts/dmp/read_debugtable.py /path/to/Db/DebugTable.dat --csv users.csv
```

**Credentials are not in DebugTable in decodable form.** The `P=` programming
records log a per-user blob, but against two ground-truth points (card 10001 and
10002) no static decode — substring, bit-field, or keyed-nibble — recovers the
card number, and two records for the same person share no structure. Card numbers
live in the encrypted `Users` table; get them via `export_via_odbc.py` or a
System Link CSV export, not from the blob.

## What we learned about the dump

* **Almost everything is DBISAM-encrypted.** `DebugTable.dat` is the only
  cleartext table (DMP left its diagnostic log unencrypted). `Users`, `Profiles`,
  `AccessCode`, `Account`, `DBINFO`, etc. are all encrypted.
* **Encryption details.** DBISAM basic header (0x00–0x1FF) stays plaintext, so
  `total_rows`/`row_size`/`total_fields` parse fine; encryption starts at 0x200
  (field descriptors + rows). Cipher is Blowfish (Elevate: MD5(password) key,
  8-byte blocks) but **position-dependent** — no repeating blocks within a table,
  yet byte-identical across tables where plaintext matches at the same offset
  ⇒ per-page/offset IV. Cracking needs DMP's compiled-in table password *and*
  the IV derivation; a wordlist over ECB/CBC-IV0 is not enough. Realistic path
  to real rows: export via System Link itself, or DMP's SDK/API.
* **`DebugTable` is the readable window.** It logs raw panel protocol traffic
  (account 0001) with user/device names embedded, e.g.
  `\u 00047"JANE DOE` (user 47) and `"DA\v 008"MAIN DOOR 1` (door 8).
* **User-number slots are recycled.** The same number is reprogrammed to a
  different person over time (slot 1010 = JANE DOE *and* SAM JONES). Track
  credentials by physical card identity, not slot — same hazard as CDVI ids.
* **User names are truncated to 16 chars** (`YAEMARI L. TAJER` vs `…TAJERA`),
  so a CDVI-style `[accessgrid]` name marker won't fit — enrollment trigger
  should key off a **Profile** (DMP's access-level concept) instead.
* **No email/phone anywhere** in the dump — AccessGrid provisioning needs an
  email source DMP panels don't hold.

## Relevant tables for the sync (all encrypted)

* `Users.dat` — user number, User Code (credential), 16-char name, active flag,
  Profile assignment. **Primary enrollment source.**
* `Profiles.dat` — access profiles (access areas + up to 4 private doors) =
  access-level / enrollment-group concept.
* `Device734.dat` / `DeviceInfo.dat` — 734 door/reader modules → door names.
* `AreaInfo.dat`, `Account.dat`, `Comm/CommPath/NetOpts` — areas, panel identity,
  connectivity. `AccessCode.dat` is the **keypad lockout code**, not credentials.
