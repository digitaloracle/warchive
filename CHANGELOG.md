# Changelog

All notable changes to `warchive`. Dates are absolute.

## 2026-06-11 — Review, cleanup, performance & features

A full review of the project produced three tiers of work: housekeeping,
a performance rebuild, and new features. The working core (`wa_search.py`)
was sound; this round hardened everything around it.

### Added

- **Hybrid retrieval — a major text-search upgrade.** The old path selected
  candidates with SQL `LIKE` substring matching (so anything lost to a typo,
  inflection, synonym, or Hebrew vowel-pointing was never retrieved), then ranked
  with BM25. The new path:
  - **FTS5 lexical index** over normalized, stemmed tokens (`message_fts`), ranked
    by SQLite's native `bm25()`. New module **`wa_normalize.py`**: NFC, niqqud /
    bidi-control stripping, Hebrew final-letter folding, and prefix **+ suffix**
    stemming — so a Hebrew query matches inflected/prefixed forms it previously
    missed. Prefix matching restores partial-word recall.
  - **Semantic vector search** (new module **`wa_embed.py`**): local, offline
    multilingual embeddings (`paraphrase-multilingual-MiniLM-L12-v2` via fastembed,
    stored in `sqlite-vec`), catching paraphrase and **cross-lingual Hebrew↔English**
    matches, in `wa_vec.db`. The full ~54k embed happens once; after that the index
    updates **incrementally** — only new messages are embedded on a rebuild
    (`wa_embed.add_index` / `indexed_ids`), turning a multi-minute rebuild into
    seconds. (CUDA/ROCm GPU isn't used — the host is AMD-on-Windows; DirectML is a
    possible future opt-in for the one-time first build.)
  - **Reciprocal Rank Fusion** of the lexical and semantic rankings (`--mode hybrid`,
    default; also `lexical` / `semantic`). Degrades gracefully to lexical-only when
    the embedding extras aren't installed (the bare pip CLI), so it never hard-fails.
  - **Query expansion** (new module **`wa_translit.py`**): Hebrew↔Latin
    transliteration variants widen recall for code-switched/transliterated terms.
  - **Snippets, conversation-context expansion (`--context N`), `--mine`/`--theirs`
    direction filter, and `--recency` ranking blend.** New MCP `search_messages`
    params: `mode`, `direction`, `context`, `recency`.
  - Mirror schema bumped to **v3** (adds the FTS index); existing mirrors rebuild
    automatically. Refactor: `query_messages()`/`_mirror_search()` now do hybrid
    retrieval; the `--no-mirror` direct path keeps the legacy substring+BM25 behavior.
- **MCP server flavor (`wa_mcp.py`)** for Claude Desktop. Desktop's skill/code
  sandbox can't reach the running WhatsApp process; an MCP server runs as a normal
  host process outside that sandbox. Exposes four tools — `search_messages`,
  `list_chats`, `refresh_cache`, `wa_status` — built on `FastMCP`, importing and
  reusing `wa_search.py` wholesale. Dependencies are contained by **uv** via PEP 723
  inline metadata + a `wa_mcp.py.lock` lockfile (no global installs). Launched by
  Desktop as `uv run --script wa_mcp.py` (stdio). Verified end-to-end with a real
  MCP stdio handshake; structured output via `dict[str, Any]` return types.
- Refactor: extracted `query_messages()` and `get_chats()` public functions from
  `main()` in `wa_search.py`, shared by the CLI and the MCP server (CLI output
  verified byte-identical to pre-refactor baselines).
- **Plaintext mirror (`wa_mirror.db`)** — the headline performance change.
  After the first decrypt, fully-resolved messages plus a `contact` table are
  cached in a plain SQLite DB next to the script (`wa_search.py` SECTION 6.7).
  Freshness is a watermark — `(mtime, size)` of `genericStorage.db` and its WAL,
  stored in a `meta` table. Warm queries read the mirror only: **no memory key
  scan, no page decryption, and they work even when WhatsApp is closed.**
  - New flags: `--refresh` (force rebuild), `--no-mirror` (bypass; the original
    direct B-tree path, kept for parity checks and debugging).
  - The mirror rebuilds automatically when new messages arrive, and refreshes
    the `from_me` direction cache at the same time.
  - Measured: `--list-chats` warm ≈ **0.12 s** (was a full decrypt + 47k-row
    scan each run); `--chat "Alice"` ≈ 0.50 s warm vs 0.87 s direct, and far
    faster on a cold start (no ~60–90 s memory scan).
- **`--csv` export** — `timestamp, direction, display, chatId, text`.
- **`--html` export** — a styled, RTL-aware transcript (sent vs received
  bubbles; `dir="auto"` for correct Hebrew/Arabic rendering).
- **`--list-chats` now shows each chat's last-message time** (both text and JSON).
- **`from_me` direction is now documented as a shipped feature** (~47% coverage,
  parsed from the IndexedDB LevelDB store) rather than "future work" — the code
  already did this; the docs were stale.
- Project scaffolding: `README.md` (quickstart), `requirements.txt`,
  `.gitignore`, `deploy.ps1`, `CHANGELOG.md` (this file), and
  `reference/README.md` (third-party attribution).

### Fixed

- **WAL salt bug — recent messages silently dropped (cut off at an earlier date).**
  `_reconstruct_pages` replayed *every* WAL frame, including stale frames left in the
  file tail from previous (checkpointed) WAL generations. An old commit frame in that
  tail was taken as the final state, truncating the database to a weeks-old snapshot.
  On the test machine this hid **6,904 of the most recent messages** (everything after
  2026-06-08). Fix: stop replay at the first frame whose salt doesn't match the WAL
  header salt, so only the current generation is applied (`db_size` 4034 → 4442;
  47,467 → 54,371 messages, newest now current to the minute). This was also the true
  cause of the old "`integrity_check` malformed / FTS pages beyond `db_size`" note.
- **`_rtl` crash that silently truncated output.** `python-bidi`'s
  `get_display` raises `AssertionError: PDI not allowed here` on messages
  containing Unicode isolate characters (U+2066–2069). This aborted the whole
  command mid-stream — e.g. the `--chat "Alice"` compact transcript was being
  cut off at 8,565 of 13,190 messages. `_rtl` now wraps the call in
  try/except and falls back to the raw text, recovering the lost messages.

### Changed

- Query filtering in the mirror path uses SQL `LIKE` substring matching, chosen
  deliberately over FTS5 so results stay **byte-for-byte identical** to the
  direct path (FTS tokenization would change partial-word and Hebrew matching).
  Verified with `--no-mirror`.
- Repository cleanup:
  - 62 research/probe scripts (`_*.py`, `memory_dump.db`, screenshots, early
    `*_decrypt.py`, `whatsapp_db_exporter.py`) moved to `archive/`.
  - Third-party ZAPiXDESK scripts (`zapix_*.ps1`) moved to `reference/`.
  - Stale 48 MB Playwright browser profile moved to `archive/wa_profile/`
    (was the bulk of the repo; can be deleted to reclaim the space).
- **Secrets redacted** from `DECRYPTION.md` (live AES keys, `clientKey`, session
  hash). Real keys now live only in the gitignored `.env`. Dump the message-DB
  key on demand with `python wa_search.py --dump-key`.
- `SKILL.md` dependency line now lists all three deps (`cryptography`,
  `python-bidi`, `python-snappy`) and documents the mirror + export flags.

### Deferred (documented as future work)

- **Group-member attribution** — *which* participant sent a given group message.
  The author JID is in the V8-serialized LevelDB object but needs a deeper
  deserializer than the current targeted `rowId` scan.
- **Reply / quoted-message context** — same constraint (V8 blob only).
- **Media indicators** — investigated and *not* implemented: only 3 of 47,467
  messages have empty text, so there is no blank-line problem to solve.

### Notes

- `wa_search.py` runs from **two locations**: the dev copy in the repo root and the
  deployed copy the `/wa` skill executes (`~/.claude/skills/wa-query/`). Run
  `deploy.ps1` after any edit — the skill uses its own copy.
- Windows only (relies on `ReadProcessMemory` / `VirtualQueryEx`).
