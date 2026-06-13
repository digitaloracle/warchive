# warchive — TODO

Effort: S (small) · M (medium) · L (large).

## In progress (this round)

### Search & retrieval quality
- [x] **Index chat & contact names in search** (S) — make chat/contact display
      names searchable so e.g. `haircut` finds a hair-salon chat by its (Hebrew)
      name even when no message body contains the word (lexical + cross-lingual
      semantic name match).
- [x] **Boolean / field query syntax** (S–M) — `from:me in:Nir before:2026-06-01`
      style tokens parsed out of the query and mapped to existing filters.
- [x] **"More like this" / semantic neighbors** (S) — given a message, return
      related ones via the existing vector index.
- [x] **Typo / fuzzy contact-name matching** (M) — a query token close (edit-
      distance) to a chat-name token surfaces that chat (one-letter typos OK).
      *Keyword-body* fuzzing (fts5vocab + edit-distance term expansion) deferred
      → backlog.
- [x] **Relative-date parsing** (S) — "last Tuesday", "two weeks ago",
      "yesterday" resolved to `--since`/`--until` in CLI + MCP.

### Coverage
- [ ] **Reply / quote context** (M) — show the message a reply quotes (needs
      extracting the quoted ref from the LevelDB V8 objects — feasibility first).
- [x] **Group-member attribution** (M) — who in a group sent each message,
      extracted from the LevelDB `author` JID-object and resolved to a name
      (~100% of group messages on the test install). Stored as sender/sender_name;
      shown in compact/JSON/MCP output.

### Packaging
- [x] **pip packaging via uv** (S–M) — `pyproject.toml` + `warchive` /
      `warchive-mcp` console entry points, built/installed with uv; semantic/mcp
      optional extras; `WARCHIVE_HOME` for state when installed.

## Backlog (picked later)

### Coverage
- [ ] **Voice-note transcription** (M–L) — local Whisper → searchable text.
- [ ] **Image OCR + media indexing** (M) — index attachments; OCR image text.
- [ ] **Reactions & edited/deleted detection** (S–M).

### Analysis & insights
- [ ] **Event/commitment extractor** (M) — pull appointments/times/addresses/
      amounts into a structured list ("when is my meeting", "haircut time").
- [ ] **"Catch me up" digest** (M) across chats for a window.
- [ ] **Conversation stats** (M) — counts over time, response times, initiators,
      active hours, top contacts/words.
- [ ] **First-class summaries tool** (M).

### Output & UX
- [ ] **Local web UI** (M–L) · **Richer HTML export** (S–M) · **TUI/REPL** (M) ·
      **Markdown/PDF export** (S).

### MCP & automation
- [ ] **More MCP tools** (S–M) — `get_conversation`, `summarize_chat`,
      `get_contacts`, `stats`.
- [ ] **Live auto-refresh daemon** (M) · **Scheduled digest** (M).

### Search
- [ ] **Keyword-body fuzzy tolerance** (M) — fts5vocab + edit-distance term
      expansion so misspelled *body* keywords still match (name fuzzing shipped).

### Platform & robustness
- [ ] **macOS / Linux support** (L).
- [ ] **CI with fixtures** (M).
- [ ] **Windows `os.replace` race hardening** (S) — retry on "Access denied"
      during concurrent refreshes.
