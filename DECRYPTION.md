# WhatsApp Desktop — Decryption & Message Access

Read-only access to live WhatsApp Desktop (Windows UWP) chat history.
No modification to the app, no QR code, no Frida. Works while WhatsApp is running.

---

## 1. Data model

WhatsApp for Windows is a UWP app. Its data lives under:

```
C:\Users\<user>\AppData\Local\Packages\
  5319275A.WhatsAppDesktop_cv1g1gvanyjgm\LocalState\sessions\
    <SESSION_HASH>\
      genericStorage.db        ← all messages (AES-256-OFB encrypted)
      genericStorage.db-wal    ← SQLite WAL (also encrypted)
      nativeSettings.db        ← contains the DB keys (also encrypted)
      contacts.db              ← phone ↔ LID mapping, display names
      contactsState.db
      abprops.db
      session.db               ← session credentials (DPAPI-NG encrypted key)
```

`SESSION_HASH` = `SHA1(clientKey)`, e.g. `A1B2C3D4E5F6...` (40 hex chars; machine-specific).

### Key databases

| Database | Key source | Contents |
|---|---|---|
| `genericStorage.db` | row 1 of `nativeSettings.db` | All messages (incoming + outgoing) |
| `contacts.db` | row 2 of `nativeSettings.db` | Phone → LID mapping, display names |
| `nativeSettings.db` | WhatsApp process memory | Holds all other DB keys |
| `session.db` | DPAPI-NG (Windows credential) | `clientKey` (session token) |

---

## 2. Encryption scheme

All SQLite databases use **SQLite SEE with AES-256-OFB** (Output Feedback Mode).

### Per-page IV construction

Each 4096-byte page is encrypted independently:

```
IV = [page_number as 4-byte little-endian] + [last 12 bytes of the encrypted page]
```

The last 12 bytes of each page are a per-page nonce stored in plaintext.

### Page-1 special case

Page 1 bytes `0x10–0x17` (the SQLite header fields: page size, format versions,
reserved bytes) are stored **unencrypted** in the SEE implementation used by WhatsApp.
After AES-OFB decryption of page 1, these 8 bytes must be restored from the ciphertext:

```python
dec[0x10:0x18] = encrypted_page[0x10:0x18]  # page 1 ONLY
```

Applying this fixup to any other page corrupts cell pointer bytes 16–23, causing
the B-tree reader to hang on malformed record headers.

### Decryption (Python)

```python
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from cryptography.hazmat.decrepit.ciphers.modes import OFB
import struct

PAGE_SIZE = 4096

def decrypt_page(key: bytes, pgno: int, page: bytes) -> bytes:
    iv  = struct.pack("<I", pgno) + page[-12:]
    dec = Cipher(algorithms.AES(key), OFB(iv)).decryptor().update(page)
    if pgno == 1:
        dec = bytearray(dec)
        dec[0x10:0x18] = page[0x10:0x18]
        dec = bytes(dec)
    return dec
```

---

## 3. Key recovery

### 3a. The full key chain (informational)

```
Windows DPAPI-NG
  └─▶ session.db AES key
        └─▶ decrypt session.db → extract clientKey (48 bytes)
              └─▶ SHA1(clientKey) = SESSION_HASH (names the session directory)
                    └─▶ ODUID (GetOfflineDeviceUniqueID via clipc.dll)
                          + clientKey
                          └─▶ PBKDF2 → nativeSettings.db key
                                └─▶ row 1 → genericStorage.db key
                                    row 2 → contacts.db key
```

**ODUID is inaccessible** from outside the app container — `clipc.dll::GetOfflineDeviceUniqueID`
requires the UWP app container context and returns an error from a regular process.

### 3b. Practical shortcut: memory scan

After WhatsApp has loaded and decrypted its databases, the AES keys sit in
its heap. A single pass over all committed readable memory regions, testing
each 8-byte-aligned 32-byte block as an AES-OFB key against page 1 of the
target database, finds both keys in ~60–90 seconds.

The entropy pre-filter skips obviously bad candidates:

```python
if cand.count(0) > 10 or len(set(cand)) < 10:
    continue   # too many zeros or too few unique bytes → not a key
```

Both keys (`genericStorage.db` and `contacts.db`) are found in a **single pass**
by testing each candidate against both databases simultaneously.

### 3c. Key values (this machine)

The recovered keys are machine-specific and are **not committed**. They live in
`.env` in the repo root (gitignored) and are loaded automatically at runtime:

```
WA_GS_KEY=<redacted — see local .env>   # decrypts genericStorage.db (messages)
WA_CT_KEY=<redacted — see local .env>   # decrypts contacts.db, abprops.db, contactsState.db
```

The `nativeSettings.db` key (used only to verify the chain), the `clientKey`,
the `webview2_static` DPAPI-NG input, and `SHA1(clientKey)` (the session
directory name) were also recovered during development. These are sensitive
session secrets and are intentionally **not reproduced here** — recover them
locally with the memory scan / `_extract_client_key.py` if needed.

Dump the current message-DB key at any time with:

```
python wa_search.py --dump-key
```

---

## 4. WAL reconstruction

WhatsApp keeps the database in WAL mode. The correct approach:

1. Decrypt all pages from the main `.db` file.
2. Walk WAL frames in order, **but only within the current generation** (see
   below). Accumulate frames into a working copy.
3. On each **commit frame** (`db_size > 0` in the frame header), snapshot the
   working copy as the current committed state and record `db_size`.
4. Return the **last committed snapshot** and its `db_size`.

### WAL generations and the salt boundary (critical)

A WAL file is **not** truncated when SQLite checkpoints it — the header's salt is
changed and new frames are written from the start, leaving stale frames from older
generations physically present in the tail of the file. Each frame header carries
the salt of the generation that wrote it (frame bytes `8:16`); a frame belongs to
the current generation only if its salt equals the WAL header's salt (header bytes
`16:24`). **Replay must stop at the first frame whose salt does not match**, exactly
as SQLite does.

Skipping this check is a real bug with a nasty symptom: the stale tail frames get
applied over live pages, and an old commit frame near the end of the file is taken
as the "last commit", silently truncating the database to a weeks-old snapshot
(messages appear to stop at an earlier date). On this machine the live WAL had 1826
frames but only the **first 256** matched the header salt; the last valid commit was
`db_size = 4442`, while the stale tail's last commit was `db_size = 4034` — the
latter dropped ~6,900 of the most recent messages. The earlier "`integrity_check`
reports malformed because the FTS root references pages beyond `db_size = 4034`"
observation was a *downstream symptom of this same bug*: those FTS pages (4103,
4107, 4235, 4242) are perfectly valid in the real `db_size = 4442` generation.

Pages beyond the (correct) `db_size` are still ignored during traversal as a
defensive measure (`_traverse_table` bounds every page reference).

---

## 5. B-tree traversal

`sqlite3` refuses to open the database (`database disk image is malformed`).
The custom reader in `wa_search.py` bypasses this by walking the B-tree directly.

### SQLite leaf page layout (type `0x0D`)

```
offset  size  field
0       1     page type (0x0D = leaf table)
1       2     first freeblock offset
3       2     number of cells
5       2     content area start offset
7       1     fragmented free bytes
8       2×N   cell pointer array (N = number of cells)
...           cell data (grows downward from top of page)
```

Each cell:
```
varint  payload_size
varint  rowid
bytes   payload (SQLite record)
```

### Record header

```
varint  header_size (includes this varint itself)
varint  col_type_0
varint  col_type_1
...
```

Type encoding: `0`=NULL, `1–6`=integers, `7`=float64, `8`=int0, `9`=int1,
`≥12 even`=BLOB of `(t-12)/2` bytes, `≥13 odd`=TEXT of `(t-13)/2` bytes.

**Safety guard:** `while off < hdr_size and off < len(payload)` — without the
`len(payload)` bound, a corrupted `hdr_size` varint causes the loop to run for
tens of millions of iterations before exhausting a 32-byte candidate block.

### message table schema

```sql
CREATE TABLE message (
  rowid     INTEGER PRIMARY KEY,
  id        TEXT,      -- internal message ID
  chatId    TEXT,      -- JID: see Section 6
  timestamp TEXT,      -- Unix seconds (or milliseconds if > 1e12)
  text      TEXT       -- message body (incoming AND outgoing)
)
```

Total rows grow over time (54,371 and counting as of 2026-06-11; the figure
depends on the live WAL generation — see §4).

---

## 6. JID formats and LID resolution

WhatsApp identifies contacts and groups via JIDs (Jabber IDs):

| Format | Meaning |
|---|---|
| `15551234567@s.whatsapp.net` | Individual — phone number |
| `15550001111-1234567890@g.us` | Group |
| `100000000000001@lid` | Linked Device ID (LID) — opaque privacy alias |

Since late 2024 WhatsApp migrated to **LID-only** message storage for
privacy. All `message.chatId` values are `@lid` or `@g.us`; no `@s.whatsapp.net`
JIDs appear in `genericStorage.db`.

### Phone → LID mapping

The mapping lives in `contacts.db` → table `UserStatuses`:

- Rows with `col3 = 'NOTIFICATION_HASH_WhatsApp.PhoneNumberUserJid'`:
  `col1` = `15551234567@s.whatsapp.net`, `col12` = display name
- Rows with `col4 = 'NOTIFICATION_HASH_WhatsApp.LidUserJid'`:
  `col17` = `100000000000001@lid`, `col12` = display name

Join is by **display name** (the only common field), collected in a single pass.

### Known contact resolutions

Cached in `.wa_contact_cache.json` in the repo root (last 20 queried):

| Phone | LID | Display name |
|---|---|---|
| +15551234567 | 100000000000001 | Alice Example |

---

## 7. Using wa_search.py

### First run (key discovery)

```
python wa_search.py --list-chats --verbose
```

WhatsApp must be running. The memory scan takes ~60–90 s, then keys are
saved to `.env` automatically. All subsequent runs are instant.

### Everyday usage

```bash
# Search by keyword
python wa_search.py "שלום"

# Filter to one contact — phone number, partial display name, or LID fragment
python wa_search.py --phone +15551234567
python wa_search.py --chat "Alice"           # partial name, loads full contacts map
python wa_search.py --chat 100000000000001   # LID fragment

# Date range filter (both --since and --until are inclusive, YYYY-MM-DD)
python wa_search.py --phone +15551234567 --since 2026-06-01
python wa_search.py --chat "Alice" --since 2026-06-08 --until 2026-06-08

# JSON output (pipe-friendly)
python wa_search.py "pizza" --json | python -m json.tool

# List all chats sorted by message count
python wa_search.py --list-chats

# Dump the DB key (for manual use or backup)
python wa_search.py --dump-key
```

### Key flags

| Flag | Purpose |
|---|---|
| `--key HEX` | Override auto-loaded `WA_GS_KEY` |
| `--contacts-key HEX` | Override auto-loaded `WA_CT_KEY` |
| `--session-dir PATH` | Override auto-detected session directory |
| `--since YYYY-MM-DD` | Show messages on or after this date (inclusive) |
| `--until YYYY-MM-DD` | Show messages on or before this date (inclusive) |
| `--csv` / `--html` | Export results as CSV or a styled HTML transcript (stdout) |
| `--refresh` | Force-rebuild the plaintext mirror (see §10) |
| `--no-mirror` | Bypass the mirror and read the encrypted DB directly |
| `--verbose` | Print timing and progress to stderr |

### Speaker identification (me vs contact)

The `genericStorage.db` `message` table has **no** direction column:

```sql
CREATE TABLE message (rowid, id TEXT, chatId TEXT, timestamp TEXT, text TEXT)
```

Direction (`fromMe: true/false`) lives in the WhatsApp Desktop IndexedDB (LevelDB
at `LocalCache\EBWebView\Default\IndexedDB\https_web.whatsapp.com_0.indexeddb.leveldb`).
`wa_search.py` parses the LevelDB SSTable/log blocks directly (SECTION 6.6) and
correlates each message's `rowId` back to the SQLite row, yielding a `from_me`
field for **~47%** of messages (compact/JSON/CSV/HTML all surface it as
`>`/`<`/sent/received). Coverage is partial because not every message object is
resident in the LevelDB store. See `_build_fromme_cache`.

**Still future work:**
- **Group-member attribution** — *which* participant sent a given group message.
  The author JID is present in the V8-serialized LevelDB object but extracting it
  reliably needs a deeper V8 deserializer than the targeted `rowId` scan we use today.
- **Reply / quoted-message context** — the quoted message id is likewise only in
  the V8 blob.

---

## 8. Key files

| File | Purpose |
|---|---|
| `wa_search.py` | Main CLI tool |
| `.env` | Persisted AES keys (`WA_GS_KEY`, `WA_CT_KEY`) |
| `wa_mirror.db` | Plaintext mirror — fast repeated queries (see §10) |
| `_fromme_cache.json` | Cached `rowId → fromMe` direction map (from LevelDB) |
| `.wa_contact_cache.json` | LRU cache: last 20 phone → LID resolutions |
| `archive/_extract_client_key.py` | DPAPI-NG → session.db → clientKey extraction (research) |
| `archive/_test_search.py` | Quick test with hardcoded key, skips memory scan (research) |

---

## 9. Lessons learned

**Apply the page-1 SEE fixup only to page 1.**
Applying `dec[0x10:0x18] = enc[0x10:0x18]` to all pages corrupts cell pointer
bytes 16–23 on every non-root leaf page. The B-tree reader then reads cell data
from garbage offsets, encounters multi-million-iteration loops in `_parse_record`,
and effectively hangs. Symptom: traversal processes ~100 pages in 160 s instead
of <1 s.

**Bound the record header loop.**
`while off < hdr_size and off < len(payload)` — the `len(payload)` guard is
essential when decryption is wrong or data is corrupted.

**ctypes HMODULE truncation.**
Always set `kernel32.LoadLibraryW.restype = ctypes.c_void_p` (and any function
returning a HANDLE) before calling it. The default `c_int` return type silently
truncates the upper 32 bits of a 64-bit handle, making `GetProcAddress` return 0.

**WAL db_size, not max page number.**
The correct database size after WAL replay is the `db_size` field of the last
commit frame *of the current generation*, not `max(pages.keys())`.

**Validate the WAL frame salt — only replay the current generation.**
A checkpointed WAL keeps stale frames from older generations in its tail (the file
is not truncated, only the header salt changes). Replaying them lets an old commit
frame clobber live pages and truncate the DB to a stale snapshot, so recent
messages silently disappear. Stop replay at the first frame whose salt (`frame[8:16]`)
differs from the WAL header salt (`header[16:24]`). This was the cause of the
"messages stop at an earlier date" bug — see §4.

---

## 10. Plaintext mirror (performance)

Decrypting + traversing the B-tree, decrypting `contacts.db`, and scanning the
LevelDB store on **every** invocation is wasteful when nothing has changed. After
the first decrypt, `wa_search.py` writes a fully-resolved **plaintext** SQLite
mirror, `wa_mirror.db`, next to the script (SECTION 6.7):

```sql
CREATE TABLE message(rowid, id, chatId, display, timestamp, ts_epoch, text, from_me);
CREATE TABLE contact(phone, lid, name);   -- phone→LID resolution, no contacts.db decrypt
CREATE TABLE meta(k, v);                  -- schema version + source watermark
CREATE VIRTUAL TABLE message_fts USING fts5(norm, tokenize='unicode61');
```

Display names and `from_me` are precomputed at build time, and `message_fts`
holds the normalized/stemmed token stream (see §11), so queries are pure SQL.

**Freshness** is a watermark = `(mtime, size)` of `genericStorage.db` and its WAL,
stored in `meta`. On each run:

- watermark matches → query the mirror directly. **No memory scan, no decryption,
  works with WhatsApp closed.** Sub-second.
- watermark differs / mirror missing → rebuild from the encrypted source (this is
  also when the `from_me` cache is refreshed, so new messages get direction data),
  then query.

```
python wa_search.py --refresh                            # force a rebuild
python wa_search.py --chat "Alice" --compact --no-mirror # legacy direct B-tree read
```

---

## 11. Retrieval (hybrid lexical + semantic)

Search (mirror path, `SECTION 6.8`) combines two retrievers and fuses them:

- **Lexical** — the `message_fts` FTS5 index, ranked by SQLite's `bm25()`. Both
  the index and the query run through `wa_normalize` (NFC; strip niqqud and bidi
  controls; fold Hebrew final letters; prefix **+ suffix** stemming), so inflected
  Hebrew forms match. Query tokens are prefix-matched and OR'd; `wa_translit`
  adds Hebrew↔Latin transliteration variants for code-switched terms.
- **Semantic** — `wa_embed` embeds every message with a local multilingual model
  (`paraphrase-multilingual-MiniLM-L12-v2` via fastembed, stored in `sqlite-vec`
  at `wa_vec.db`) and retrieves by cosine similarity, catching paraphrase and
  cross-lingual Hebrew↔English matches. Built once per mirror change; **optional**
  (skipped if the embedding deps are absent).
- **Fusion** — Reciprocal Rank Fusion (`1/(k+rank)`, k=60) merges the two ranked
  lists (`--mode hybrid`). `--mode lexical` / `--mode semantic` select one.

Non-text filters (chat/phone/date/`from_me`) constrain both retrievers. A `LIKE`
substring fallback guarantees results are never empty when FTS finds nothing.
`--context N` pulls neighbouring messages around each hit; `--recency` blends a
recency ranking into the fusion. The `--no-mirror` path retains the original
substring + Python-BM25 behavior for debugging/parity.
