#!/usr/bin/env python3
"""
wa_search.py — Read-only WhatsApp Desktop chat search.

Decrypts genericStorage.db live from the running WhatsApp process
by scanning process memory for the AES-OFB key, then walks the
SQLite B-tree directly (bypasses sqlite3 integrity check on the WAL).

Usage:
  wa_search.py "keyword"
  wa_search.py "keyword" --chat "name or JID fragment"
  wa_search.py "keyword" --phone +972501234567
  wa_search.py "keyword" --since 2025-01-01
  wa_search.py --list-chats
  wa_search.py "keyword" --json
  wa_search.py --dump-key          # print recovered DB key and exit
"""

import argparse, csv, ctypes, datetime, difflib, html, io, json, math, os, re, sqlite3, struct, sys

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
    from cryptography.hazmat.decrepit.ciphers.modes import OFB
except ImportError:
    sys.exit("pip install cryptography")

try:
    from bidi.algorithm import get_display as _bidi_display
    def _rtl(text: str) -> str:
        if not text:
            return text
        try:
            return _bidi_display(text)
        except Exception:
            # python-bidi asserts on some isolate/PDI sequences (U+2066–2069);
            # never let display reordering crash the whole output.
            return text
except ImportError:
    def _rtl(text: str) -> str:  # type: ignore[misc]
        return text

# ── Retrieval modules (local; degrade gracefully if absent) ─────────────────
try:
    import wa_normalize
except Exception:                       # pragma: no cover
    wa_normalize = None
try:
    import wa_translit
except Exception:                       # pragma: no cover
    wa_translit = None
try:
    import wa_embed                     # heavy deps are lazy-loaded inside it
except Exception:                       # pragma: no cover
    wa_embed = None


def _norm(text):
    """Normalize text for matching (niqqud/bidi/final-letter folding) if available."""
    if wa_normalize:
        return wa_normalize.normalize(text or "")
    return (text or "").lower()


def _norm_tokens(text):
    """Tokenize+stem for the FTS index/query (Hebrew prefix+suffix aware) if available."""
    if wa_normalize:
        return wa_normalize.tokens(text or "")
    return [w for w in re.findall(r"[֐-׿\w]+", (text or "").lower()) if len(w) >= 2]


# ── Config ─────────────────────────────────────────────────────────────────
SESSION_GLOB = (
    r"C:\Users\{user}\AppData\Local\Packages"
    r"\5319275A.WhatsAppDesktop_cv1g1gvanyjgm\LocalState\sessions"
)
SQLITE_MAGIC = b"SQLite format 3\x00"
PAGE_SIZE    = 4096
WAL_HDR      = 32
FRAME_HDR    = 24

_cached_key         = None
_cached_pages       = None
_CONTACTS_KEY_ROW2  = None
_cached_phone_map   = None
_cached_phone_names = None
_cached_name_rows   = None   # lid → display name, full contacts.db load

# ── Persistent state ────────────────────────────────────────────────────────
_SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
# State (keys, mirror, caches) lives in _DATA_DIR. Defaults to the script's
# directory — so source checkouts, the /wa skill, and the MCP server keep their
# existing files — but a pip-installed copy (where the script sits in read-only
# site-packages) should set WARCHIVE_HOME to a writable folder.
_DATA_DIR          = os.environ.get("WARCHIVE_HOME") or _SCRIPT_DIR
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
except OSError:
    pass
_ENV_FILE          = os.path.join(_DATA_DIR, ".env")
_CONTACT_CACHE     = os.path.join(_DATA_DIR, ".wa_contact_cache.json")
_CONTACT_CACHE_MAX = 20


def _load_env():
    """Populate _cached_key / _CONTACTS_KEY_ROW2 from .env on first import."""
    global _cached_key, _CONTACTS_KEY_ROW2
    if not os.path.exists(_ENV_FILE):
        return
    with open(_ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            try:
                b = bytes.fromhex(v.strip())
            except ValueError:
                continue
            if len(b) != 32:
                continue
            if k == "WA_GS_KEY" and _cached_key is None:
                _cached_key = b
            elif k == "WA_CT_KEY" and _CONTACTS_KEY_ROW2 is None:
                _CONTACTS_KEY_ROW2 = b


def _save_env():
    """Persist current keys to .env (creates or updates in place)."""
    keep = []
    if os.path.exists(_ENV_FILE):
        with open(_ENV_FILE, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("WA_GS_KEY=") or s.startswith("WA_CT_KEY="):
                    continue
                keep.append(line.rstrip())
    if _cached_key is not None:
        keep.append(f"WA_GS_KEY={_cached_key.hex()}")
    if _CONTACTS_KEY_ROW2 is not None:
        keep.append(f"WA_CT_KEY={_CONTACTS_KEY_ROW2.hex()}")
    with open(_ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(keep) + "\n")


def _read_contact_cache():
    """Return list of {phone, lid, name} most-recent-first."""
    if not os.path.exists(_CONTACT_CACHE):
        return []
    try:
        with open(_CONTACT_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write_contact_cache(entries):
    try:
        with open(_CONTACT_CACHE, "w", encoding="utf-8") as f:
            json.dump(entries[:_CONTACT_CACHE_MAX], f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _cache_contact(phone_digits, lid, name):
    entries = [e for e in _read_contact_cache() if e.get("phone") != phone_digits]
    entries.insert(0, {"phone": phone_digits, "lid": lid, "name": name or ""})
    _write_contact_cache(entries)


def _lookup_phone_in_cache(phone_digits):
    """Return (lid, name) if cached, else (None, None)."""
    for e in _read_contact_cache():
        if e.get("phone") == phone_digits:
            return e.get("lid"), e.get("name")
    return None, None


def _contact_display_map():
    """Return {lid → display_name} from the contact cache."""
    return {e["lid"]: e["name"] for e in _read_contact_cache() if e.get("lid") and e.get("name")}


# Load keys from .env at import time so every invocation benefits immediately.
_load_env()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — AES-OFB key recovery from WhatsApp process memory
# ══════════════════════════════════════════════════════════════════════════════

def _find_wa_pid():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                    ("th32ProcessID", ctypes.c_ulong),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                    ("th32ParentProcessID", ctypes.c_ulong),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong),
                    ("szExeFile", ctypes.c_char * 260)]

    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    pe = PE32(); pe.dwSize = ctypes.sizeof(PE32)
    pid = None
    if kernel32.Process32First(snap, ctypes.byref(pe)):
        while True:
            if b"WhatsApp" in pe.szExeFile:
                pid = pe.th32ProcessID
            if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                break
    kernel32.CloseHandle(snap)
    return pid


def _read_mem(h, addr, size):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf   = (ctypes.c_char * size)()
    nread = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(ctypes.c_void_p(h), ctypes.c_void_p(addr),
                                    buf, size, ctypes.byref(nread))
    return bytes(buf[:nread.value]) if ok and nread.value else None


def find_db_keys(db_paths, verbose=False):
    """Single-pass memory scan for AES-OFB keys for one or more SQLite dbs.
    Returns dict {db_path: key_bytes} for each path whose key was found."""
    targets = {}
    for p in db_paths:
        if not os.path.exists(p):
            continue
        with open(p, "rb") as f:
            page1 = f.read(PAGE_SIZE)
        iv = struct.pack("<I", 1) + page1[-12:]
        targets[p] = (iv, page1[:16])

    if not targets:
        return {}

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p

    pid = _find_wa_pid()
    if not pid:
        raise RuntimeError("WhatsApp process not found — is it running?")
    h = kernel32.OpenProcess(0x0410, False, pid)
    if not h:
        raise RuntimeError(
            f"Cannot open WhatsApp process (PID {pid}); run as Administrator?")

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", ctypes.c_ulong), ("RegionSize", ctypes.c_size_t),
                    ("State", ctypes.c_ulong), ("Protect", ctypes.c_ulong),
                    ("Type", ctypes.c_ulong)]

    addr   = 0
    mbi    = MBI()
    found  = {}
    remain = dict(targets)

    if verbose:
        print(f"[key-scan] Scanning WhatsApp memory for {len(remain)} key(s)...",
              file=sys.stderr)

    while remain:
        ret = kernel32.VirtualQueryEx(ctypes.c_void_p(h), ctypes.c_void_p(addr),
                                      ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not ret:
            break
        sz   = mbi.RegionSize
        prot = mbi.Protect & 0xFF
        readable = (mbi.State == 0x1000 and prot not in (0x01, 0x00)
                    and not (mbi.Protect & 0x100) and sz <= 64 * 1024 * 1024)
        if readable:
            chunk = _read_mem(h, addr, sz)
            if chunk:
                for off in range(0, len(chunk) - 32, 8):
                    cand = chunk[off:off + 32]
                    if cand.count(0) > 10 or len(set(cand)) < 10:
                        continue
                    for path, (iv, magic) in list(remain.items()):
                        dec = Cipher(algorithms.AES(cand), OFB(iv)).decryptor().update(magic)
                        if dec == SQLITE_MAGIC:
                            found[path] = cand
                            del remain[path]
                            if verbose:
                                print(f"[key-scan] {os.path.basename(path)}: "
                                      f"{cand.hex()} @ {addr+off:#x}", file=sys.stderr)
                            break
        addr += sz
        if addr >= 0x7FFFFFFFFFFF:
            break

    kernel32.CloseHandle(ctypes.c_void_p(h))
    return found


def find_db_key(db_path, verbose=False):
    """Scan WhatsApp process memory for the AES-OFB key for a single db."""
    result = find_db_keys([db_path], verbose=verbose)
    if db_path not in result:
        raise RuntimeError(
            "DB key not found in WhatsApp memory — is WhatsApp running with chats loaded?")
    return result[db_path]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — AES-OFB page decryption + WAL reconstruction
# ══════════════════════════════════════════════════════════════════════════════

def _decrypt_page(key, pgno, page):
    iv = struct.pack("<I", pgno) + bytes(page[-12:])
    return Cipher(algorithms.AES(key), OFB(iv)).decryptor().update(page)


def _reconstruct_pages(key, db_src, wal_src):
    """Decrypt all pages and apply WAL frames; returns (pages_dict, db_size_pages)."""
    with open(db_src, "rb") as f:
        db_raw = f.read()
    with open(wal_src, "rb") as f:
        wal_raw = f.read()

    pages = {}
    for i in range(len(db_raw) // PAGE_SIZE):
        p    = db_raw[i * PAGE_SIZE:(i + 1) * PAGE_SIZE]
        pgno = i + 1
        dec  = bytearray(_decrypt_page(key, pgno, p))
        if pgno == 1:
            dec[0x10:0x18] = p[0x10:0x18]  # page-1 only: these bytes are unencrypted in SEE
        pages[pgno] = dec

    off     = WAL_HDR
    last_sz = len(db_raw) // PAGE_SIZE
    acc     = dict(pages)

    # Replay ONLY the current WAL generation. Each frame header carries the salt of
    # the generation that wrote it (frame bytes 8:16); frames whose salt differs
    # from the WAL header's salt (header bytes 16:24) are stale leftovers from a
    # previous, already-checkpointed generation that SQLite has not yet overwritten.
    # Applying them lets an old commit frame clobber live pages and truncates the
    # database to a stale snapshot — the cause of messages appearing to stop at an
    # earlier date. Stop at the first salt mismatch, exactly as SQLite does.
    if len(wal_raw) >= WAL_HDR:
        wal_magic = struct.unpack_from(">I", wal_raw, 0)[0]
        hdr_salt  = wal_raw[16:24]
        if wal_magic in (0x377F0682, 0x377F0683):   # valid WAL; else: no real WAL
            while off + FRAME_HDR + PAGE_SIZE <= len(wal_raw):
                fhdr = wal_raw[off:off + FRAME_HDR]
                if fhdr[8:16] != hdr_salt:
                    break   # first stale frame ends the valid generation
                pg_enc = wal_raw[off + FRAME_HDR:off + FRAME_HDR + PAGE_SIZE]
                pgno   = struct.unpack_from(">I", fhdr, 0)[0]
                db_sz  = struct.unpack_from(">I", fhdr, 4)[0]
                dec    = bytearray(_decrypt_page(key, pgno, pg_enc))
                if pgno == 1:
                    dec[0x10:0x18] = pg_enc[0x10:0x18]
                acc[pgno] = dec
                if db_sz > 0:
                    last_sz = db_sz
                    pages   = dict(acc)
                off += FRAME_HDR + PAGE_SIZE

    return pages, last_sz


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — SQLite B-tree reader (bypasses integrity check)
# ══════════════════════════════════════════════════════════════════════════════

def _read_varint(data, off):
    val = 0
    for i in range(9):
        if off + i >= len(data):
            break
        b = data[off + i]
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            return val, off + i + 1
    return val, off + 9


def _parse_record(payload, col_names):
    """Parse a SQLite record payload into a dict."""
    hdr_sz, off = _read_varint(payload, 0)
    col_types   = []
    while off < hdr_sz and off < len(payload):
        t, off = _read_varint(payload, off)
        col_types.append(t)

    val_off = hdr_sz
    result  = {}
    for ci, ct in enumerate(col_types):
        name = col_names[ci] if ci < len(col_names) else f"col{ci}"
        if ct == 0:
            val, sz = None, 0
        elif ct == 1:
            val = struct.unpack_from(">b", payload, val_off)[0]; sz = 1
        elif ct == 2:
            val = struct.unpack_from(">h", payload, val_off)[0]; sz = 2
        elif ct == 3:
            val = int.from_bytes(payload[val_off:val_off + 3], "big", signed=True); sz = 3
        elif ct == 4:
            val = struct.unpack_from(">i", payload, val_off)[0]; sz = 4
        elif ct == 5:
            val = int.from_bytes(payload[val_off:val_off + 6], "big", signed=True); sz = 6
        elif ct == 6:
            val = struct.unpack_from(">q", payload, val_off)[0]; sz = 8
        elif ct == 7:
            val = struct.unpack_from(">d", payload, val_off)[0]; sz = 8
        elif ct == 8:
            val, sz = 0, 0
        elif ct == 9:
            val, sz = 1, 0
        elif ct >= 12 and ct % 2 == 0:
            sz  = (ct - 12) // 2
            val = payload[val_off:val_off + sz]
        elif ct >= 13 and ct % 2 == 1:
            sz  = (ct - 13) // 2
            val = payload[val_off:val_off + sz].decode("utf-8", "replace")
        else:
            val, sz = None, 0
        result[name] = val
        val_off += sz
        if val_off > len(payload):
            break
    return result


def _read_leaf_rows(page_bytes, col_names, page_hdr_offset=0, reserved_sz=0):
    """Yield record dicts from a leaf-table page (type 0x0d)."""
    d     = page_bytes
    base  = page_hdr_offset
    ptype = d[base]
    if ptype != 0x0D:
        return
    n_cells = struct.unpack_from(">H", d, base + 3)[0]
    usable  = PAGE_SIZE - reserved_sz

    for i in range(n_cells):
        ptr_off  = base + 8 + i * 2
        if ptr_off + 2 > len(d):
            break
        cell_off = struct.unpack_from(">H", d, ptr_off)[0]
        if cell_off == 0 or cell_off >= len(d):
            continue
        try:
            payload_sz, p = _read_varint(d, cell_off)
            rowid,      p = _read_varint(d, p)
            if 0 < payload_sz <= usable:
                payload = d[p:p + payload_sz]
                rec = _parse_record(payload, col_names)
                rec["_rowid"] = rowid
                yield rec
        except Exception:
            continue


def _traverse_table(pages, db_size, root_pgno, col_names):
    """Depth-first B-tree traversal; yields record dicts from all leaf pages."""
    stack   = [root_pgno]
    visited = set()

    reserved_sz = pages[1][20] if 1 in pages and len(pages[1]) > 20 else 0
    usable      = PAGE_SIZE - reserved_sz

    while stack:
        pgno = stack.pop()
        if pgno in visited or pgno < 1 or pgno > db_size:
            continue
        visited.add(pgno)
        data = pages.get(pgno)
        if not data:
            continue
        ptype = data[0]

        if ptype == 0x0D:   # leaf table page
            yield from _read_leaf_rows(bytes(data), col_names,
                                       page_hdr_offset=0, reserved_sz=reserved_sz)

        elif ptype == 0x05:  # interior table page
            n_cells   = struct.unpack_from(">H", data, 3)[0]
            rightmost = struct.unpack_from(">I", data, 8)[0]
            if 1 <= rightmost <= db_size:
                stack.append(rightmost)
            for i in range(n_cells):
                ptr_off  = 12 + i * 2
                if ptr_off + 2 > usable:
                    break
                cell_off = struct.unpack_from(">H", data, ptr_off)[0]
                if cell_off == 0 or cell_off + 4 > usable:
                    continue
                child = struct.unpack_from(">I", data, cell_off)[0]
                if 1 <= child <= db_size:
                    stack.append(child)


def _get_message_root_page(pages):
    """Return root page of the 'message' table from sqlite_master (page 1)."""
    data = pages.get(1)
    if not data:
        return 2
    # page 1 has a 100-byte database header before the B-tree page header
    base    = 100
    ptype   = data[base]
    if ptype != 0x0D:
        return 2
    n_cells = struct.unpack_from(">H", data, base + 3)[0]
    col_names = ["type", "name", "tbl_name", "rootpage", "sql"]
    for i in range(n_cells):
        ptr_off  = base + 8 + i * 2
        if ptr_off + 2 > len(data):
            break
        cell_off = struct.unpack_from(">H", data, ptr_off)[0]
        if cell_off == 0 or cell_off >= PAGE_SIZE:
            continue
        try:
            payload_sz, p = _read_varint(data, cell_off)
            _, p = _read_varint(data, p)
            payload = bytes(data[p:p + payload_sz])
            rec = _parse_record(payload, col_names)
            if rec.get("name") == "message" and rec.get("rootpage"):
                return int(rec["rootpage"])
        except Exception:
            continue
    return 2


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Session / DB path discovery
# ══════════════════════════════════════════════════════════════════════════════

def _find_session_dir():
    import glob as _glob
    user    = os.environ.get("USERNAME", os.environ.get("USER", ""))
    base    = SESSION_GLOB.format(user=user)
    pattern = os.path.join(base, "*", "genericStorage.db")
    matches = _glob.glob(pattern)
    if not matches:
        raise RuntimeError(f"genericStorage.db not found under {base}")
    return os.path.dirname(max(matches, key=os.path.getsize))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4b — contacts.db phone → LID resolution
# ══════════════════════════════════════════════════════════════════════════════



def _get_contacts_key(session_dir, verbose=False):
    """Find the AES-OFB key for contacts.db by scanning WhatsApp memory."""
    db = os.path.join(session_dir, "contacts.db")
    if not os.path.exists(db):
        return None
    return find_db_key(db, verbose=verbose)


def _load_phone_map(session_dir, verbose=False):
    """Build digits → LID map from contacts.db UserStatuses table."""
    global _cached_phone_map, _cached_phone_names, _CONTACTS_KEY_ROW2, _cached_name_rows
    if _cached_phone_map is not None:
        return _cached_phone_map

    if _CONTACTS_KEY_ROW2 is None:
        _CONTACTS_KEY_ROW2 = _get_contacts_key(session_dir, verbose=verbose)
    if _CONTACTS_KEY_ROW2 is None:
        _cached_phone_map = {}
        return _cached_phone_map

    db  = os.path.join(session_dir, "contacts.db")
    wal = db + "-wal"
    if not os.path.exists(db):
        _cached_phone_map = {}
        return _cached_phone_map

    if verbose:
        print("[*] Loading phone→LID map from contacts.db...", file=sys.stderr)

    pages, db_size = _reconstruct_pages(_CONTACTS_KEY_ROW2, db,
                                        wal if os.path.exists(wal) else db)

    # Single pass: accumulate both sides then join by display name.
    phone_rows = {}   # digits → name
    lid_rows   = {}   # name  → lid
    name_rows  = {}   # lid   → name  (for _contact_display_map enrichment)

    for row in _traverse_table(pages, db_size, 3, []):
        key_type = row.get("col3") or row.get("col4") or ""
        name     = row.get("col12") or row.get("col11") or row.get("col10") or ""
        if key_type == "NOTIFICATION_HASH_WhatsApp.PhoneNumberUserJid":
            jid = row.get("col1") or ""
            if "@s.whatsapp.net" in jid and name:
                phone_rows[jid.split("@")[0]] = name
        elif key_type == "NOTIFICATION_HASH_WhatsApp.LidUserJid":
            lid_jid = row.get("col17") or ""
            if "@lid" in lid_jid and name:
                lid  = lid_jid.split("@")[0]
                lid_rows[name] = lid
                name_rows[lid] = name

    phone_to_lid  = {digits: lid_rows[name]
                     for digits, name in phone_rows.items()
                     if name in lid_rows}
    phone_to_name = {digits: name
                     for digits, name in phone_rows.items()
                     if name in lid_rows}

    _cached_phone_map   = phone_to_lid
    _cached_phone_names = phone_to_name
    _cached_name_rows   = name_rows          # lid → name for all contacts
    if verbose:
        print(f"[*] Loaded {len(phone_to_lid)} phone→LID mappings "
              f"({len(name_rows)} LID names total)", file=sys.stderr)
    return phone_to_lid


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — High-level message iteration
# ══════════════════════════════════════════════════════════════════════════════

_MSG_COLS = ["_pk", "id", "chatId", "timestamp", "text"]


def _load_pages(session_dir, verbose=False, need_contacts_key=False):
    global _cached_key, _cached_pages, _CONTACTS_KEY_ROW2
    gs_db  = os.path.join(session_dir, "genericStorage.db")
    gs_wal = os.path.join(session_dir, "genericStorage.db-wal")

    if _cached_key is None:
        if verbose:
            print("[*] Recovering DB key(s) from WhatsApp memory...", file=sys.stderr)
        if need_contacts_key and _CONTACTS_KEY_ROW2 is None:
            ct_db = os.path.join(session_dir, "contacts.db")
            keys  = find_db_keys([gs_db, ct_db], verbose=verbose)
            _cached_key        = keys.get(gs_db)
            _CONTACTS_KEY_ROW2 = keys.get(ct_db)
            if _cached_key is None:
                raise RuntimeError(
                    "genericStorage.db key not found in WhatsApp memory")
        else:
            _cached_key = find_db_key(gs_db, verbose=verbose)
        if verbose:
            print(f"[*] Key: {_cached_key.hex()}", file=sys.stderr)
        _save_env()   # persist newly found key(s) to .env

    if _cached_pages is None:
        if verbose:
            print("[*] Reconstructing genericStorage.db pages...", file=sys.stderr)
        _cached_pages = _reconstruct_pages(_cached_key, gs_db, gs_wal)

    return _cached_pages[0], _cached_pages[1]


def iter_messages(session_dir, verbose=False, need_contacts_key=False):
    """Yield message dicts: {id, chatId, timestamp, text, _rowid}."""
    pages, db_size = _load_pages(session_dir, verbose=verbose,
                                 need_contacts_key=need_contacts_key)
    root_pg = _get_message_root_page(pages)
    if verbose:
        print(f"[*] message root_page={root_pg}, db_size={db_size}", file=sys.stderr)
    yield from _traverse_table(pages, db_size, root_pg, _MSG_COLS)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — Formatting helpers
# ══════════════════════════════════════════════════════════════════════════════

def _jid_to_display(jid):
    if not jid:
        return "?"
    if jid.endswith("@g.us"):
        return f"Group:{jid.split('@')[0]}"
    if jid.endswith("@s.whatsapp.net"):
        return f"+{jid.split('@')[0]}"
    if jid.endswith("@lid"):
        return f"LID:{jid.split('@')[0]}"
    return jid


def _ts_to_str(ts_val):
    try:
        ts = int(str(ts_val))
        if ts > 1_000_000_000_000:
            ts //= 1000
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts_val) if ts_val is not None else "?"


def _ts_to_epoch(ts_val):
    try:
        ts = int(str(ts_val))
        if ts > 1_000_000_000_000:
            ts //= 1000
        return ts
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6.5 — BM25 relevance ranking
# ══════════════════════════════════════════════════════════════════════════════

_HE_PREFIXES = ('וה', 'שה', 'בה', 'כה', 'לה', 'מה', 'ב', 'כ', 'ל', 'ש', 'ה', 'ו', 'מ')


def _tokenize(text):
    words = re.findall(r'[֐-׿\w]+', text.lower())
    result = []
    for w in words:
        if len(w) < 2:
            continue
        stem = w
        for p in _HE_PREFIXES:
            if w.startswith(p) and len(w) > len(p) + 1:
                stem = w[len(p):]
                break
        result.append(stem)
        if stem != w:
            result.append(w)
    return result


def _bm25_rank(query, messages, top_k=None, k1=1.5, b=0.75):
    q_tokens = set(_tokenize(query))
    if not q_tokens:
        return messages[:top_k] if top_k else messages
    docs  = [_tokenize(m.get("text") or "") for m in messages]
    N     = len(docs)
    avgdl = sum(len(d) for d in docs) / max(N, 1)
    df = {}
    for doc in docs:
        for t in set(doc):
            df[t] = df.get(t, 0) + 1
    scored = []
    for msg, doc in zip(messages, docs):
        tf_map = {}
        for t in doc:
            tf_map[t] = tf_map.get(t, 0) + 1
        score = 0.0
        dl = len(doc)
        for qt in q_tokens:
            n_qt = df.get(qt, 0)
            if not n_qt:
                continue
            idf    = math.log((N - n_qt + 0.5) / (n_qt + 0.5) + 1.0)
            tf     = tf_map.get(qt, 0)
            tf_norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))
            score  += idf * tf_norm
        if score > 0:
            scored.append((score, msg))
    scored.sort(key=lambda x: -x[0])
    result = [m for _, m in scored]
    return result[:top_k] if top_k else result


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6.6 — fromMe extraction from IndexedDB LevelDB
# ══════════════════════════════════════════════════════════════════════════════

try:
    import snappy as _snappy
    _ldb_decomp = _snappy.uncompress
except ImportError:
    try:
        import cramjam as _cramjam
        _ldb_decomp = lambda d: bytes(_cramjam.snappy.decompress_raw(d))
    except ImportError:
        _ldb_decomp = None

_LDB_MAGIC = 0xdb4775248b80fb57
_LDB_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    r"Packages\5319275A.WhatsAppDesktop_cv1g1gvanyjgm"
    r"\LocalCache\EBWebView\Default\IndexedDB"
    r"\https_web.whatsapp.com_0.indexeddb.leveldb",
)
_FROMME_CACHE_FILE = os.path.join(_DATA_DIR, "_fromme_cache.json")
_cached_fromme = None   # dict[str, bool] once loaded

_WA_KEY_RE = re.compile(r'^(true|false)_', re.IGNORECASE)
_ROWID_PAT = re.compile(rb'\x22\x05rowId\x49')


def _ldb_vi(buf):
    r = i = 0
    while True:
        b = buf.read(1)
        if not b:
            return None
        b = b[0]; r |= (b & 0x7f) << (i * 7); i += 1
        if not (b & 0x80):
            return r


def _ldb_vat(data, off):
    r = i = 0
    while True:
        if off + i >= len(data):
            return 0, 1
        b = data[off + i]; r |= (b & 0x7f) << (i * 7); i += 1
        if not (b & 0x80):
            return r, i


def _ldb_idb_prefix(key):
    if not key or len(key) < 4:
        return None
    h = key[0]
    db_sz = ((h >> 5) & 7) + 1; os_sz = ((h >> 2) & 7) + 1; idx_sz = (h & 3) + 1
    plen = 1 + db_sz + os_sz + idx_sz
    if len(key) < plen:
        return None
    idx_id = int.from_bytes(key[1 + db_sz + os_sz:plen], "little")
    return idx_id, key[plen:]


def _ldb_decode_key(ukey):
    """IDB string key: \x01 + varint(char_count) + UTF-16-BE."""
    if not ukey or ukey[0] != 0x01:
        return None
    n, s = _ldb_vat(ukey, 1)
    start = 1 + s; end = start + n * 2
    if end > len(ukey):
        return None
    try:
        return ukey[start:end].decode("utf-16-be")
    except Exception:
        return None


def _ldb_extract(key_str, val):
    if not _WA_KEY_RE.match(key_str):
        return None
    from_me = key_str.lower().startswith("true_")
    mro = _ROWID_PAT.search(val)
    if not mro:
        return None
    zz, _ = _ldb_vat(val, mro.end())
    row_id = (zz >> 1) ^ -(zz & 1)
    if row_id <= 0:
        return None
    return row_id, from_me


def _ldb_walk_block(block):
    if len(block) < 8:
        return
    rc = struct.unpack_from("<I", block, len(block) - 4)[0]
    roff = len(block) - (rc + 1) * 4
    if not (0 <= roff < len(block)):
        return
    prev = b""; buf = io.BytesIO(block)
    while buf.tell() < roff:
        sh = _ldb_vi(buf); nsh = _ldb_vi(buf); vl = _ldb_vi(buf)
        if None in (sh, nsh, vl):
            break
        k = prev[:sh] + buf.read(nsh); prev = k
        yield k, buf.read(vl)


def _ldb_walk_sst(raw):
    if len(raw) < 48:
        return
    if struct.unpack_from("<Q", raw, len(raw) - 8)[0] != _LDB_MAGIC:
        return
    fb = io.BytesIO(raw[len(raw) - 48:])
    _ldb_vi(fb); _ldb_vi(fb)
    ioff = _ldb_vi(fb); isz = _ldb_vi(fb)
    if None in (ioff, isz) or ioff + isz >= len(raw):
        return
    iblk = raw[ioff:ioff + isz]
    if ioff + isz < len(raw) and raw[ioff + isz] == 1:
        if not _ldb_decomp:
            return
        try:
            iblk = _ldb_decomp(iblk)
        except Exception:
            return
    for _, v in _ldb_walk_block(iblk):
        boff, s1 = _ldb_vat(v, 0); bsz, _ = _ldb_vat(v, s1)
        if not bsz or boff + bsz >= len(raw):
            continue
        btype = raw[boff + bsz]; blk = raw[boff:boff + bsz]
        if btype == 1:
            if not _ldb_decomp:
                continue
            try:
                blk = _ldb_decomp(blk)
            except Exception:
                continue
        yield from _ldb_walk_block(blk)


def _ldb_walk_log(raw):
    HEADER = 7; BLOCK = 32768
    record = b""; off = 0
    while off < len(raw):
        rem = BLOCK - (off % BLOCK)
        if rem < HEADER:
            off += rem; continue
        if off + HEADER > len(raw):
            break
        length = struct.unpack_from("<H", raw, off + 4)[0]
        rtype = raw[off + 6]; off += HEADER
        if off + length > len(raw):
            break
        chunk = raw[off:off + length]; off += length
        if rtype == 0:
            record = b""; continue
        if rtype == 2:
            record = chunk; continue
        if rtype == 3:
            record += chunk; continue
        record = chunk if rtype == 1 else record + chunk
        if len(record) < 12:
            record = b""; continue
        count = struct.unpack_from("<I", record, 8)[0]
        pos = 12
        for _ in range(count):
            if pos >= len(record):
                break
            rt = record[pos]; pos += 1
            kl, s = _ldb_vat(record, pos); pos += s
            if pos + kl > len(record):
                break
            k = record[pos:pos + kl]; pos += kl
            if rt == 0:
                continue
            vl, s = _ldb_vat(record, pos); pos += s
            if pos + vl > len(record):
                break
            yield k, record[pos:pos + vl]; pos += vl
        record = b""


def _build_fromme_cache():
    """Scan LevelDB files and return {str(row_id): bool} dict."""
    if not os.path.isdir(_LDB_DIR):
        return {}
    result = {}
    files = sorted(
        [p for p in os.listdir(_LDB_DIR) if p.endswith(".ldb")
         or p.endswith(".log")],
        key=lambda p: (0 if p.endswith(".ldb") else 1, p),
    )
    for fname in files:
        fp = os.path.join(_LDB_DIR, fname)
        try:
            raw = open(fp, "rb").read()
        except OSError:
            continue
        walker = _ldb_walk_sst(raw) if fname.endswith(".ldb") else _ldb_walk_log(raw)
        for ik, val in walker:
            uk = ik[:-8] if len(ik) >= 8 else ik
            p = _ldb_idb_prefix(uk)
            if not p:
                continue
            idx_id, ukey = p
            if idx_id != 1:
                continue
            key_str = _ldb_decode_key(ukey)
            if not key_str:
                continue
            r = _ldb_extract(key_str, val)
            if r is None:
                continue
            row_id, from_me = r
            result[str(row_id)] = from_me
    return result


def _load_fromme_cache():
    global _cached_fromme
    if _cached_fromme is not None:
        return _cached_fromme
    if os.path.exists(_FROMME_CACHE_FILE):
        try:
            _cached_fromme = json.loads(
                open(_FROMME_CACHE_FILE, encoding="utf-8").read())
            return _cached_fromme
        except Exception:
            pass
    _cached_fromme = _build_fromme_cache()
    if _cached_fromme:
        try:
            open(_FROMME_CACHE_FILE, "w", encoding="utf-8").write(
                json.dumps(_cached_fromme))
        except Exception:
            pass
    return _cached_fromme


def _get_fromme(msg_id):
    """Return True (outgoing), False (incoming), or None (unknown)."""
    if not msg_id:
        return None
    cache = _load_fromme_cache()
    val = cache.get(str(msg_id))
    return val  # None if not found


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6.7 — plaintext SQLite mirror (fast repeated queries)
# ══════════════════════════════════════════════════════════════════════════════
#
# Every invocation otherwise re-scans WhatsApp memory for the key, re-decrypts
# ~4,000 pages, re-decrypts contacts.db, and re-scans the LevelDB store. We cache
# the fully-resolved result in a plaintext SQLite DB next to this script and only
# rebuild it when the encrypted source actually changes (watermark = mtime+size of
# genericStorage.db and its WAL). A warm query touches none of the encrypted source
# and works even when WhatsApp is closed.

_MIRROR_PATH           = os.path.join(_DATA_DIR, "wa_mirror.db")
_VEC_PATH              = os.path.join(_DATA_DIR, "wa_vec.db")   # semantic index
# Bump when the build logic changes in a way that invalidates existing mirrors.
# v2: WAL salt-generation fix — v1 mirrors were truncated to a stale snapshot.
# v3: added FTS5 (normalized/stemmed) index for lexical retrieval.
_MIRROR_SCHEMA_VERSION = "3"
# Separate from the mirror schema: the embedding index only needs rebuilding when
# the *embedding* logic changes, not when we add SQL columns. Keeping this
# independent means a mirror schema bump no longer forces a full re-embed.
_VEC_SCHEMA_VERSION = "1"


def _source_watermark(session_dir):
    parts = {}
    for name in ("genericStorage.db", "genericStorage.db-wal"):
        p = os.path.join(session_dir, name)
        try:
            st = os.stat(p)
            parts[name] = [int(st.st_mtime), st.st_size]
        except OSError:
            parts[name] = None
    return json.dumps(parts, sort_keys=True)


def _mirror_is_fresh(session_dir):
    if not os.path.exists(_MIRROR_PATH):
        return False
    try:
        con = sqlite3.connect(_MIRROR_PATH)
        wm  = con.execute("SELECT v FROM meta WHERE k='watermark'").fetchone()
        ver = con.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
        con.close()
    except sqlite3.Error:
        return False
    if not wm or not ver or ver[0] != _MIRROR_SCHEMA_VERSION:
        return False
    return wm[0] == _source_watermark(session_dir)


def _refresh_fromme_cache(verbose=False):
    """Rebuild the from_me direction cache from LevelDB and overwrite the json."""
    global _cached_fromme
    if verbose:
        print("[*] Rebuilding from_me cache from LevelDB...", file=sys.stderr)
    _cached_fromme = _build_fromme_cache()
    if _cached_fromme:
        try:
            with open(_FROMME_CACHE_FILE, "w", encoding="utf-8") as f:
                f.write(json.dumps(_cached_fromme))
        except Exception:
            pass
    return _cached_fromme


def _build_mirror(session_dir, verbose=False):
    """(Re)build the plaintext mirror from the encrypted source. Needs WhatsApp running."""
    # Resolve display names + phone map (decrypts contacts.db once).
    _load_phone_map(session_dir, verbose=verbose)
    display_map = dict(_contact_display_map())
    if _cached_name_rows:
        for _lid, _name in _cached_name_rows.items():
            display_map.setdefault(_lid, _name)

    def _disp(jid):
        if jid.endswith("@lid"):
            lid = jid.split("@")[0]
            if lid in display_map:
                return display_map[lid]
        return _jid_to_display(jid)

    # Direction data changes when new messages arrive — refresh alongside the mirror.
    _refresh_fromme_cache(verbose=verbose)

    tmp = _MIRROR_PATH + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.executescript("""
        CREATE TABLE message(rowid INTEGER PRIMARY KEY, id TEXT, chatId TEXT,
            display TEXT, timestamp TEXT, ts_epoch INTEGER, text TEXT, from_me INTEGER);
        CREATE INDEX idx_msg_ts   ON message(ts_epoch);
        CREATE INDEX idx_msg_chat ON message(chatId);
        CREATE TABLE contact(phone TEXT PRIMARY KEY, lid TEXT, name TEXT);
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
        -- Lexical index over normalized + stemmed tokens (Hebrew-aware). The
        -- 'norm' column holds whitespace-joined wa_normalize.tokens(text), so
        -- FTS token matches align with the same stemming used at query time.
        CREATE VIRTUAL TABLE message_fts USING fts5(norm, tokenize='unicode61');
    """)

    def _msg_insert(rows):
        con.executemany("INSERT OR REPLACE INTO message VALUES (?,?,?,?,?,?,?,?)", rows)

    def _fts_insert(rows):
        con.executemany("INSERT INTO message_fts(rowid, norm) VALUES (?,?)", rows)

    batch, fts_batch = [], []
    n = 0
    for msg in iter_messages(session_dir, verbose=verbose, need_contacts_key=False):
        chat_id = msg.get("chatId") or ""
        mid     = msg.get("id", "")
        rowid   = msg.get("_rowid")
        text    = msg.get("text") or ""
        fm      = _get_fromme(mid)
        batch.append((
            rowid, str(mid), chat_id, _disp(chat_id),
            _ts_to_str(msg.get("timestamp")), _ts_to_epoch(msg.get("timestamp")),
            text,
            1 if fm is True else 0 if fm is False else None,
        ))
        fts_batch.append((rowid, " ".join(_norm_tokens(text))))
        n += 1
        if len(batch) >= 5000:
            _msg_insert(batch); _fts_insert(fts_batch)
            batch.clear(); fts_batch.clear()
    if batch:
        _msg_insert(batch); _fts_insert(fts_batch)

    pm = _cached_phone_map or {}
    pn = _cached_phone_names or {}
    con.executemany("INSERT OR REPLACE INTO contact VALUES (?,?,?)",
                    [(d, lid, pn.get(d, "")) for d, lid in pm.items()])

    con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)",
                (_MIRROR_SCHEMA_VERSION,))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('watermark', ?)",
                (_source_watermark(session_dir),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', ?)",
                (datetime.datetime.now().isoformat(timespec="seconds"),))
    con.commit()
    con.close()
    os.replace(tmp, _MIRROR_PATH)
    if verbose:
        print(f"[mirror] built {n} messages -> {_MIRROR_PATH}", file=sys.stderr)

    # Build the semantic vector index too, if embeddings are available (optional).
    try:
        _ensure_vec_index(verbose=verbose)
    except Exception as e:                                   # pragma: no cover
        if verbose:
            print(f"[vec] index build skipped ({e})", file=sys.stderr)


def _ensure_mirror(session_dir, force=False, verbose=False):
    """Build or refresh the mirror if needed. Returns True if a mirror is usable."""
    if force or not _mirror_is_fresh(session_dir):
        if verbose:
            print("[mirror] forced rebuild" if force
                  else "[mirror] stale or missing — rebuilding from encrypted source",
                  file=sys.stderr)
        _build_mirror(session_dir, verbose=verbose)
    elif verbose:
        print("[mirror] up to date — skipping decrypt", file=sys.stderr)
    return True


def _mirror_lookup_phone(phone_digits):
    """Resolve a phone number to (lid, name) from the mirror's contact table."""
    try:
        con = sqlite3.connect(_MIRROR_PATH)
        row = con.execute("SELECT lid, name FROM contact WHERE phone=?",
                          (phone_digits,)).fetchone()
        con.close()
    except sqlite3.Error:
        return None, None
    return (row[0], row[1]) if row else (None, None)


def _mirror_list_chats():
    """Return [(jid, display, count, last_ts), ...] from the mirror."""
    con = sqlite3.connect(_MIRROR_PATH)
    rows = con.execute(
        "SELECT chatId, MAX(display), COUNT(*), MAX(ts_epoch) "
        "FROM message GROUP BY chatId").fetchall()
    con.close()
    return rows


# ── Hybrid retrieval: lexical FTS5 + semantic vectors, fused with RRF ────────

def _ensure_vec_index(verbose=False):
    """Build/refresh the semantic vector index from the mirror, if embeddings are
    available. Cached by the mirror's watermark. Returns True if usable."""
    if not (wa_embed and getattr(wa_embed, "EMBED_AVAILABLE", False)):
        return False
    try:
        con = sqlite3.connect(_MIRROR_PATH)
        wm = con.execute("SELECT v FROM meta WHERE k='watermark'").fetchone()
    except sqlite3.Error:
        return False
    # Fingerprint is watermark + the EMBEDDING schema (not the mirror schema), so
    # adding SQL columns never forces a re-embed — only changes to how we embed do.
    watermark = (wm[0] if wm else "") + "|" + _VEC_SCHEMA_VERSION
    fp_path = _VEC_PATH + ".fp"
    have_fp = open(fp_path, encoding="utf-8").read() if os.path.exists(fp_path) else None
    have_count = wa_embed.index_count(_VEC_PATH)
    if have_fp == watermark and have_count > 0:
        con.close()
        return True   # fully up to date

    rows = con.execute(
        "SELECT rowid, text FROM message WHERE text IS NOT NULL AND text <> ''"
    ).fetchall()
    con.close()

    # Messages are append-mostly, so a source change usually means a handful of
    # NEW rows. Embed only those (seconds); reserve the full ~minutes-long build
    # for the first run or an embedding-schema change.
    schema_changed = (have_fp is None) or (have_fp.rsplit("|", 1)[-1] != _VEC_SCHEMA_VERSION)
    if have_count == 0 or schema_changed:
        if verbose:
            print("[vec] full semantic index build (one-time)...", file=sys.stderr)
        wa_embed.build_index(rows, _VEC_PATH, verbose=verbose)
    else:
        if verbose:
            print("[vec] incremental semantic index update...", file=sys.stderr)
        added = wa_embed.add_index(rows, _VEC_PATH, verbose=verbose)
        if verbose:
            print(f"[vec] {added} new vector(s) added", file=sys.stderr)
    try:
        with open(fp_path, "w", encoding="utf-8") as f:
            f.write(watermark)
    except OSError:
        pass
    return True


def _filter_clause(lid_filter, chat_lower, since_epoch, until_epoch, from_me, alias=""):
    """SQL WHERE fragment + params for the non-text filters (optionally aliased)."""
    a = (alias + ".") if alias else ""
    where, params = [], []
    if since_epoch:
        where.append(f"{a}ts_epoch >= ?"); params.append(since_epoch)
    if until_epoch:
        where.append(f"{a}ts_epoch <= ?"); params.append(until_epoch)
    if lid_filter:
        where.append(f"{a}chatId LIKE ?"); params.append(lid_filter + "@%")
    if chat_lower:
        where.append(f"(LOWER({a}display) LIKE ? OR LOWER({a}chatId) LIKE ?)")
        params += ["%" + chat_lower + "%", "%" + chat_lower + "%"]
    if from_me is True:
        where.append(f"{a}from_me = 1")
    elif from_me is False:
        where.append(f"{a}from_me = 0")
    return (" AND ".join(where), params)


def _fts_match(query_text):
    """FTS5 MATCH string from normalized+expanded query tokens (prefix, OR'd)."""
    variants = [query_text]
    if wa_translit:
        try:
            variants = wa_translit.expand_query(query_text) or [query_text]
        except Exception:
            pass
    terms, seen = [], set()
    for v in variants:
        for t in _norm_tokens(v):
            t = re.sub(r"[^\w֐-׿]", "", t)        # keep only token chars (FTS-safe)
            if len(t) >= 2 and t not in seen:
                seen.add(t); terms.append(t)
    return " OR ".join(t + "*" for t in terms)    # prefix match for partial-word recall


def _allowed_ids(con, fwhere, fparams):
    """Rowid set permitted by the non-text filters (None == no restriction)."""
    if not fwhere:
        return None
    return {r[0] for r in con.execute(
        "SELECT rowid FROM message WHERE " + fwhere, fparams)}


def _fts_search(con, query_text, fwhere_m, fparams_m, limit):
    """Lexical candidate rowids, best-first by bm25."""
    match = _fts_match(query_text)
    if not match:
        return []
    sql = ["SELECT message_fts.rowid FROM message_fts"]
    params = [match]
    if fwhere_m:
        sql.append("JOIN message m ON m.rowid = message_fts.rowid")
    sql.append("WHERE message_fts MATCH ?")
    if fwhere_m:
        sql.append("AND " + fwhere_m); params += fparams_m
    sql.append("ORDER BY bm25(message_fts) LIMIT ?"); params.append(limit)
    try:
        return [r[0] for r in con.execute(" ".join(sql), params)]
    except sqlite3.Error:
        return []


def _semantic_ids(query_text, allowed, limit):
    """Semantic candidate rowids, best-first; [] if embeddings unavailable."""
    if not (wa_embed and getattr(wa_embed, "EMBED_AVAILABLE", False)):
        return []
    try:
        if not _ensure_vec_index():
            return []
        hits = wa_embed.search(query_text, _VEC_PATH, top_k=limit)
    except Exception:
        return []
    ids = [rid for rid, _score in hits]
    if allowed is not None:
        ids = [r for r in ids if r in allowed]
    return ids


def _rrf(rankings, k=60):
    """Reciprocal Rank Fusion of best-first rowid lists -> fused order."""
    scores = {}
    for ranking in rankings:
        for i, rid in enumerate(ranking):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + i + 1)
    return sorted(scores, key=lambda r: -scores[r])


def _like_fallback(con, query_terms, fwhere, fparams, limit):
    """Old substring matcher — safety net so we never regress to zero hits."""
    if not query_terms:
        return []
    where = ["(" + " OR ".join(["LOWER(text) LIKE ?"] * len(query_terms)) + ")"]
    params = ["%" + t + "%" for t in query_terms]
    if fwhere:
        where.append(fwhere); params += fparams
    params.append(limit)
    return [r[0] for r in con.execute(
        "SELECT rowid FROM message WHERE " + " AND ".join(where)
        + " ORDER BY ts_epoch DESC LIMIT ?", params)]


def _snippet(text, query_text, width=160):
    """Short excerpt of the original text around the first matched query word."""
    if not text:
        return ""
    low = text.lower()
    terms = [t for t in re.split(r"\s+", (query_text or "").lower().strip())
             if len(t) >= 2]
    pos = min((low.find(t) for t in terms if t in low), default=-1)
    if pos < 0:
        s = text[:width].replace("\n", " ")
        return s + ("…" if len(text) > width else "")
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    return (("…" if start > 0 else "")
            + text[start:end].replace("\n", " ")
            + ("…" if end < len(text) else ""))


def _row_to_result(r, query_text=""):
    fm = r["from_me"]
    return {
        "_rowid":    r["rowid"],
        "id":        r["id"],
        "chatId":    r["chatId"],
        "display":   r["display"],
        "timestamp": r["timestamp"],
        "ts_epoch":  r["ts_epoch"],
        "text":      r["text"],
        "from_me":   True if fm == 1 else False if fm == 0 else None,
        "snippet":   _snippet(r["text"], query_text) if query_text else "",
    }


def _fetch_rows(con, rowids):
    """Fetch message rows for an ordered rowid list, preserving that order."""
    if not rowids:
        return []
    qmarks = ",".join("?" * len(rowids))
    by_id = {r["rowid"]: r for r in con.execute(
        "SELECT rowid,id,chatId,display,timestamp,ts_epoch,text,from_me "
        f"FROM message WHERE rowid IN ({qmarks})", rowids)}
    return [by_id[rid] for rid in rowids if rid in by_id]


def _name_chat_candidates(con, query_text, fwhere, fparams, limit, *, sem_threshold=0.55):
    """Recent message rowids from chats whose DISPLAY NAME matches the query —
    lexically (shared normalized tokens) and, when embeddings exist, semantically
    (cosine over chat names, so 'haircut' surfaces a 'hair styling' chat, even
    cross-lingually). Respects the active non-text filters. [] if nothing matches."""
    chats = [(r[0], r[1] or "") for r in con.execute(
        "SELECT chatId, MAX(display) FROM message GROUP BY chatId")]
    if not chats:
        return []
    matched = set()
    qtokens = set(_norm_tokens(query_text))
    if qtokens:
        for cid, disp in chats:
            name_toks = set(_norm_tokens(disp))
            if qtokens & name_toks:                       # exact token overlap
                matched.add(cid)
                continue
            # typo tolerance: a query token close to a name token (one-letter typos).
            for qt in qtokens:
                if len(qt) >= 4 and difflib.get_close_matches(qt, name_toks, n=1, cutoff=0.8):
                    matched.add(cid)
                    break
    if wa_embed and getattr(wa_embed, "EMBED_AVAILABLE", False):
        try:
            for i, score in wa_embed.rank_names(query_text, [d for _, d in chats], top_k=4):
                if score >= sem_threshold:
                    matched.add(chats[i][0])
        except Exception:
            pass
    if not matched:
        return []
    qm = ",".join("?" * len(matched))
    where = [f"chatId IN ({qm})"]
    params = list(matched)
    if fwhere:
        where.append(fwhere)
        params += fparams
    params.append(limit)
    return [r[0] for r in con.execute(
        "SELECT rowid FROM message WHERE " + " AND ".join(where)
        + " ORDER BY ts_epoch DESC LIMIT ?", params)]


def _mirror_search(lid_filter, phone_filter, chat_lower, query_terms,
                   since_epoch, until_epoch, *, query_text="", mode="hybrid",
                   from_me=None, top_k=None, recency=False):
    """Hybrid retrieval over the mirror: lexical FTS5 + semantic vectors, fused.

    No query text -> filtered, chronological listing (legacy behavior). With a
    query, candidates come from FTS (bm25) and — when embeddings are available
    and mode allows — the vector index, fused with Reciprocal Rank Fusion.
    mode ∈ {hybrid, lexical, semantic}."""
    con = sqlite3.connect(_MIRROR_PATH)
    con.row_factory = sqlite3.Row

    def _phone_ok(r):
        if not phone_filter:
            return True
        return phone_filter in "".join(c for c in r["chatId"] if c.isdigit())

    fwhere, fparams = _filter_clause(lid_filter, chat_lower, since_epoch,
                                     until_epoch, from_me)

    if not (query_text and query_text.strip()):
        sql = "SELECT rowid,id,chatId,display,timestamp,ts_epoch,text,from_me FROM message"
        if fwhere:
            sql += " WHERE " + fwhere
        sql += " ORDER BY ts_epoch"
        rows = [r for r in con.execute(sql, fparams) if _phone_ok(r)]
        if top_k:
            rows = rows[-top_k:]
        con.close()
        return [_row_to_result(r) for r in rows]

    pool = max((top_k or 200) * 5, 500)
    fwhere_m, fparams_m = _filter_clause(lid_filter, chat_lower, since_epoch,
                                         until_epoch, from_me, alias="m")
    lex = _fts_search(con, query_text, fwhere_m, fparams_m, pool) \
        if mode in ("hybrid", "lexical") else []
    sem = _semantic_ids(query_text, _allowed_ids(con, fwhere, fparams), pool) \
        if mode in ("hybrid", "semantic") else []

    # Chat/contact-name matches: when the query names a chat (e.g. a person or a
    # business), surface that chat's recent messages even if no body matches —
    # lexically, and cross-lingually when embeddings exist (haircut→"hair styling").
    name_ids = _name_chat_candidates(con, query_text, fwhere, fparams, pool)

    if mode == "lexical":
        primary = [lex]
    elif mode == "semantic":
        primary = [sem or lex]                    # degrade to lexical if no embeddings
    else:
        primary = [x for x in (lex, sem) if x]
    lists = [x for x in (primary + [name_ids]) if x]
    ranked = _rrf(lists) if len(lists) > 1 else (lists[0] if lists else [])

    if not ranked:                                # safety net: old substring matcher
        ranked = _like_fallback(con, query_terms, fwhere, fparams, pool)

    if recency and ranked:
        qm = ",".join("?" * len(ranked))
        tsmap = {r[0]: r[1] for r in con.execute(
            f"SELECT rowid, ts_epoch FROM message WHERE rowid IN ({qm})", ranked)}
        recency_rank = sorted(ranked, key=lambda rid: -tsmap.get(rid, 0))
        ranked = _rrf([ranked, recency_rank])

    rows = [r for r in _fetch_rows(con, ranked) if _phone_ok(r)]
    if top_k:
        rows = rows[:top_k]
    con.close()
    return [_row_to_result(r, query_text) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — CLI entry point
# ══════════════════════════════════════════════════════════════════════════════

_HTML_CSS = """
  body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#0b141a;
       color:#e9edef;margin:0;padding:24px;}
  h1{font-size:18px;font-weight:600;margin:0 0 4px;}
  .meta{color:#8696a0;font-size:13px;margin-bottom:20px;}
  .chat{margin:28px 0 8px;font-weight:600;color:#53bdeb;border-bottom:1px solid #222d34;
        padding-bottom:6px;}
  .msg{max-width:75%;margin:6px 0;padding:7px 11px;border-radius:8px;
       background:#202c33;width:fit-content;}
  .msg.sent{margin-left:auto;background:#005c4b;}
  .msg.unknown{background:#182229;}
  .t{display:block;font-size:11px;color:#8696a0;margin-top:3px;}
  .body{white-space:pre-wrap;word-wrap:break-word;}
"""


def _emit_html(results):
    out = sys.stdout
    out.write("<!doctype html><html><head><meta charset='utf-8'>")
    out.write("<title>WhatsApp transcript</title><style>" + _HTML_CSS + "</style></head><body>")
    out.write(f"<h1>WhatsApp transcript</h1><div class='meta'>{len(results)} message(s) · "
              f"generated {datetime.datetime.now():%Y-%m-%d %H:%M}</div>")
    prev_chat = None
    for r in results:
        if r["chatId"] != prev_chat:
            out.write(f"<div class='chat' dir='auto'>{html.escape(r['display'])} "
                      f"<span class='t'>[{html.escape(r['chatId'])}]</span></div>")
            prev_chat = r["chatId"]
        fm  = r.get("from_me")
        cls = "sent" if fm is True else "unknown" if fm is None else "recv"
        body = html.escape(r["text"] or "")
        out.write(f"<div class='msg {cls}'><span class='body' dir='auto'>{body}</span>"
                  f"<span class='t'>{html.escape(r['timestamp'])}</span></div>")
    out.write("</body></html>\n")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — public query API (shared by the CLI and the MCP server)
# ══════════════════════════════════════════════════════════════════════════════

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
             "saturday": 5, "sunday": 6, "mon": 0, "tue": 1, "tues": 1, "wed": 2,
             "thu": 3, "thur": 4, "thurs": 4, "fri": 4, "sat": 5, "sun": 6}
_MONTHS = {}
for _i, _m in enumerate(["january", "february", "march", "april", "may", "june",
                         "july", "august", "september", "october", "november",
                         "december"], 1):
    _MONTHS[_m] = _i
    _MONTHS[_m[:3]] = _i


def _parse_date(s, end_of_day=False):
    """Parse a date to epoch seconds. Accepts 'YYYY-MM-DD' or natural phrases:
    today, yesterday, tomorrow, 'N days/weeks/months/years ago', this/last
    week|month|year, 'last <weekday>' or a bare weekday, or a month name.
    `end_of_day` returns 23:59:59 of that day (inclusive range end).
    Raises ValueError if unrecognised."""
    s = (s or "").strip().lower()
    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    def out(d):
        if end_of_day:
            d = d + datetime.timedelta(days=1) - datetime.timedelta(seconds=1)
        return int(d.timestamp())

    try:
        return out(datetime.datetime.strptime(s, "%Y-%m-%d"))
    except ValueError:
        pass

    if s == "today":
        return out(today)
    if s == "yesterday":
        return out(today - datetime.timedelta(days=1))
    if s == "tomorrow":
        return out(today + datetime.timedelta(days=1))

    m = re.fullmatch(r"(\d+)\s+(day|week|month|year)s?\s+ago", s)
    if m:
        n = int(m.group(1))
        days = n * {"day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)]
        return out(today - datetime.timedelta(days=days))

    if s == "this week":
        return out(today - datetime.timedelta(days=today.weekday()))
    if s == "last week":
        return out(today - datetime.timedelta(days=today.weekday() + 7))
    if s == "this month":
        return out(today.replace(day=1))
    if s == "last month":
        return out((today.replace(day=1) - datetime.timedelta(days=1)).replace(day=1))
    if s == "this year":
        return out(today.replace(month=1, day=1))

    m = re.fullmatch(r"(?:last\s+|this\s+)?(" + "|".join(_WEEKDAYS) + r")", s)
    if m:
        delta = (today.weekday() - _WEEKDAYS[m.group(1)]) % 7
        return out(today - datetime.timedelta(days=delta or 7))

    if s in _MONTHS:
        cand = today.replace(month=_MONTHS[s], day=1)
        if cand > today:
            cand = cand.replace(year=cand.year - 1)
        return out(cand)

    raise ValueError(f"unrecognised date: {s!r}")


_FIELD_RE = re.compile(r'(?:^|\s)(from|in|chat|before|after|since|until):'
                       r'("[^"]+"|\S+)', re.IGNORECASE)


def _parse_query_fields(query):
    """Pull field tokens out of a query string and map them to filters.

    Supports `from:me|them`, `in:NAME` / `chat:NAME`, `before:`/`until:` and
    `after:`/`since:` (dates may be relative). Returns (clean_query, fields)
    where fields ⊆ {chat, since, until, from_me}. Quoted values keep spaces."""
    if not query:
        return query, {}
    fields = {}

    def repl(m):
        key, val = m.group(1).lower(), m.group(2).strip('"')
        if key == "from":
            v = val.lower()
            fields["from_me"] = (True if v in ("me", "self", "i", "mine")
                                 else False if v in ("them", "other", "theirs") else None)
        elif key in ("in", "chat"):
            fields["chat"] = val
        elif key in ("before", "until"):
            fields["until"] = val
        elif key in ("after", "since"):
            fields["since"] = val
        return " "

    clean = re.sub(r"\s+", " ", _FIELD_RE.sub(repl, query)).strip()
    return clean, fields


def _build_display_map():
    """LID → display-name map from the contact cache + full contacts load (direct path)."""
    display_map = dict(_contact_display_map())
    if _cached_name_rows:
        for _lid, _name in _cached_name_rows.items():
            display_map.setdefault(_lid, _name)
    return display_map


def _resolve_display(jid, display_map):
    if jid.endswith("@lid"):
        lid = jid.split("@")[0]
        if lid in display_map:
            return display_map[lid]
    return _jid_to_display(jid)


def _ensure_mirror_soft(session_dir, refresh, verbose):
    """Ensure the mirror; downgrade to direct read if it can't be built. Returns use_mirror."""
    try:
        _ensure_mirror(session_dir, force=refresh, verbose=verbose)
        return True
    except Exception as e:
        if os.path.exists(_MIRROR_PATH):
            print(f"[mirror] refresh failed ({e}); serving existing mirror", file=sys.stderr)
            return True
        if verbose:
            print(f"[mirror] build failed ({e}); falling back to direct read", file=sys.stderr)
        return False


def query_messages(query=None, chat=None, phone=None, since=None, until=None,
                   top_k=None, *, mode="hybrid", from_me=None, context=0,
                   recency=False, session_dir=None, use_mirror=True,
                   refresh=False, verbose=False):
    """Search WhatsApp messages and return a list of result dicts:
    {id, chatId, display, timestamp, ts_epoch, text, from_me, snippet, _rowid}.

    `since`/`until` are 'YYYY-MM-DD' strings (ValueError on bad format).
    `mode` ∈ {hybrid, lexical, semantic} (mirror path). `from_me` True/False/None
    filters by direction. `context` adds N neighbouring messages around each hit.
    `recency` blends recency into ranking. Shared by the CLI and the MCP server."""
    # Field tokens in the query (from:me, in:Alice, before:yesterday, …) fill in
    # any filter not already set explicitly by the caller.
    if query:
        query, _fields = _parse_query_fields(query)
        query = query or None
        if chat is None:
            chat = _fields.get("chat")
        if since is None:
            since = _fields.get("since")
        if until is None:
            until = _fields.get("until")
        if from_me is None and "from_me" in _fields:
            from_me = _fields["from_me"]

    session_dir = session_dir or _find_session_dir()
    if use_mirror:
        use_mirror = _ensure_mirror_soft(session_dir, refresh, verbose)

    # Resolve --phone to a LID filter (cache → mirror contact table → contacts.db).
    phone_filter = None
    lid_filter   = None
    if phone:
        phone_digits = "".join(c for c in phone if c.isdigit())
        cached_lid, _cached_name = _lookup_phone_in_cache(phone_digits)
        if cached_lid:
            lid_filter = cached_lid
        elif use_mirror:
            m_lid, m_name = _mirror_lookup_phone(phone_digits)
            if m_lid:
                lid_filter = m_lid
                _cache_contact(phone_digits, m_lid, m_name)
            else:
                phone_filter = phone_digits
        else:
            phone_map = _load_phone_map(session_dir, verbose=verbose)
            if phone_digits in phone_map:
                lid_filter = phone_map[phone_digits]
                _cache_contact(phone_digits, lid_filter,
                               (_cached_phone_names or {}).get(phone_digits, ""))
            else:
                phone_filter = phone_digits

    since_epoch = _parse_date(since) if since else 0
    until_epoch = _parse_date(until, end_of_day=True) if until else 0

    chat_lower  = chat.lower() if chat else None
    query_terms = [t for t in re.split(r"\s+", query.lower().strip()) if t] if query else []

    if use_mirror:
        results = _mirror_search(
            lid_filter, phone_filter, chat_lower, query_terms,
            since_epoch, until_epoch, query_text=query or "", mode=mode,
            from_me=from_me, top_k=top_k, recency=recency)
        if context and query and query.strip():
            results = _expand_context(results, context)
        return results   # mirror results are already ranked + capped

    # Direct (no-mirror) fallback — substring + BM25, kept for parity/debugging.
    if chat:
        _load_phone_map(session_dir, verbose=verbose)
    display_map = _build_display_map()
    results = []
    for msg in iter_messages(session_dir, verbose=verbose,
                             need_contacts_key=bool(phone or chat)):
        text    = msg.get("text") or ""
        chat_id = msg.get("chatId") or ""
        ts_ep   = _ts_to_epoch(msg.get("timestamp"))
        if query_terms:
            tl = text.lower()
            if not any(t in tl for t in query_terms):
                continue
        if lid_filter:
            if not chat_id.startswith(lid_filter + "@"):
                continue
        elif phone_filter:
            num = "".join(c for c in chat_id if c.isdigit())
            if phone_filter not in num:
                continue
        if chat_lower:
            disp = _resolve_display(chat_id, display_map).lower()
            if chat_lower not in disp and chat_lower not in chat_id.lower():
                continue
        if since_epoch and ts_ep < since_epoch:
            continue
        if until_epoch and ts_ep > until_epoch:
            continue
        fmv = _get_fromme(msg.get("id"))
        if from_me is True and fmv is not True:
            continue
        if from_me is False and fmv is not False:
            continue
        results.append({
            "id":        msg.get("id", ""),
            "chatId":    chat_id,
            "display":   _resolve_display(chat_id, display_map),
            "timestamp": _ts_to_str(msg.get("timestamp")),
            "ts_epoch":  ts_ep,
            "text":      text,
            "from_me":   fmv,
        })

    results.sort(key=lambda r: r["ts_epoch"])
    if query and results:
        results = _bm25_rank(query, results, top_k=top_k)
    elif top_k:
        results = results[-top_k:]
    return results


def _expand_context(hits, n):
    """Add up to n messages before/after each hit within the same chat, marking
    which rows are matches. Returns chat-grouped chronological order. Bounded:
    only the first 40 hits are expanded to avoid context blow-ups."""
    if not hits:
        return hits
    hit_keys = {(h["chatId"], h["_rowid"]) for h in hits if h.get("_rowid") is not None}
    out = {}
    con = sqlite3.connect(_MIRROR_PATH)
    con.row_factory = sqlite3.Row
    for h in hits:
        out[(h["chatId"], h.get("_rowid"))] = h
    expandable = [h for h in hits if h.get("_rowid") is not None][:40]
    for h in expandable:
        cid, rid = h["chatId"], h["_rowid"]
        neigh = con.execute(
            "SELECT rowid,id,chatId,display,timestamp,ts_epoch,text,from_me FROM ("
            "  SELECT * FROM message WHERE chatId=? AND rowid<=? ORDER BY rowid DESC LIMIT ?"
            ") UNION "
            "SELECT rowid,id,chatId,display,timestamp,ts_epoch,text,from_me FROM ("
            "  SELECT * FROM message WHERE chatId=? AND rowid>? ORDER BY rowid ASC LIMIT ?"
            ")", (cid, rid, n + 1, cid, rid, n)).fetchall()
        for r in neigh:
            key = (r["chatId"], r["rowid"])
            if key not in out:
                out[key] = _row_to_result(r)
    con.close()
    merged = list(out.values())
    for m in merged:
        m["is_match"] = (m["chatId"], m.get("_rowid")) in hit_keys
    merged.sort(key=lambda r: (r["chatId"], r["ts_epoch"]))
    return merged


def get_chats(*, session_dir=None, use_mirror=True, refresh=False, verbose=False):
    """Return [{jid, display, messages, last}] sorted by message count desc."""
    session_dir = session_dir or _find_session_dir()
    if use_mirror:
        use_mirror = _ensure_mirror_soft(session_dir, refresh, verbose)

    if use_mirror:
        rows = [(j, d or _jid_to_display(j), c, lt)
                for (j, d, c, lt) in _mirror_list_chats()]
    else:
        display_map = _build_display_map()
        agg = {}
        for msg in iter_messages(session_dir, verbose=verbose):
            jid = msg.get("chatId") or ""
            ts  = _ts_to_epoch(msg.get("timestamp"))
            slot = agg.setdefault(jid, [0, 0])
            slot[0] += 1
            if ts > slot[1]:
                slot[1] = ts
        rows = [(j, _resolve_display(j, display_map), c, lt)
                for j, (c, lt) in agg.items()]
    rows.sort(key=lambda x: -x[2])
    return [{"jid": j, "display": d, "messages": c,
             "last": (_ts_to_str(lt) if lt else None)}
            for (j, d, c, lt) in rows]


def find_similar(ref, top_k=10, *, session_dir=None, use_mirror=True, verbose=False):
    """Return messages semantically similar to a reference — given by its message
    id, its rowid, or a raw text string — best-first. Uses the existing vector
    index; returns [] if the semantic extras aren't available."""
    if not (wa_embed and getattr(wa_embed, "EMBED_AVAILABLE", False)):
        return []
    session_dir = session_dir or _find_session_dir()
    if use_mirror:
        _ensure_mirror_soft(session_dir, False, verbose)
    if not os.path.exists(_MIRROR_PATH):
        return []
    con = sqlite3.connect(_MIRROR_PATH)
    con.row_factory = sqlite3.Row
    text, self_rowid = str(ref), None
    row = con.execute("SELECT rowid, text FROM message WHERE id=? LIMIT 1",
                      (str(ref),)).fetchone()
    if not row and str(ref).isdigit():
        row = con.execute("SELECT rowid, text FROM message WHERE rowid=? LIMIT 1",
                          (int(ref),)).fetchone()
    if row:
        text, self_rowid = row["text"], row["rowid"]
    if not (text and text.strip()) or not _ensure_vec_index(verbose=verbose):
        con.close()
        return []
    hits = wa_embed.search(text, _VEC_PATH, top_k=top_k + 1)
    ids = [rid for rid, _score in hits if rid != self_rowid][:top_k]
    rows = _fetch_rows(con, ids)
    con.close()
    return [_row_to_result(r, text) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — CLI entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    # Ensure UTF-8 output on Windows (handles emoji in display names).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description="Search WhatsApp Desktop chat history (read-only, live DB)")
    ap.add_argument("query",         nargs="?", default=None,
                    help="Keyword to search in message text")
    ap.add_argument("--chat",        metavar="NAME",
                    help="Filter by chat: JID fragment or display name (case-insensitive)")
    ap.add_argument("--phone",       metavar="PHONE",
                    help="Filter by phone number, e.g. +972501234567")
    ap.add_argument("--since",       metavar="YYYY-MM-DD",
                    help="Show only messages on or after this date")
    ap.add_argument("--until",       metavar="YYYY-MM-DD",
                    help="Show only messages before or on this date (inclusive)")
    ap.add_argument("--list-chats",  action="store_true",
                    help="List all unique chats and exit")
    ap.add_argument("--json",        action="store_true",
                    help="Output as JSON array")
    ap.add_argument("--csv",         action="store_true",
                    help="Output as CSV (timestamp, direction, display, chatId, text)")
    ap.add_argument("--html",        action="store_true",
                    help="Output as a styled HTML transcript (redirect to a .html file)")
    ap.add_argument("--dump-key",    action="store_true",
                    help="Print recovered AES key (hex) and exit")
    ap.add_argument("--key",          metavar="HEX",
                    help="Use this hex-encoded AES key instead of scanning memory "
                         "(get it once with --dump-key)")
    ap.add_argument("--contacts-key", metavar="HEX",
                    help="AES key for contacts.db (needed by --phone; "
                         "found automatically via memory scan if omitted)")
    ap.add_argument("--session-dir", metavar="DIR",
                    help="Override auto-detected session directory")
    ap.add_argument("--top-k",       metavar="N", type=int,
                    help="Return only top N results (BM25-ranked if query given, "
                         "else most recent N)")
    ap.add_argument("--compact",     action="store_true",
                    help="One-line output: [timestamp] text (token-efficient for LLM use)")
    ap.add_argument("--refresh",     action="store_true",
                    help="Force-rebuild the plaintext mirror from the encrypted source")
    ap.add_argument("--no-mirror",   action="store_true",
                    help="Bypass the plaintext mirror and read the encrypted DB directly")
    ap.add_argument("--mode",        choices=["hybrid", "lexical", "semantic"],
                    default="hybrid",
                    help="Retrieval mode for keyword queries (default: hybrid "
                         "lexical+semantic; semantic needs the embedding extras)")
    ap.add_argument("--mine",        action="store_true",
                    help="Only messages you sent")
    ap.add_argument("--theirs",      action="store_true",
                    help="Only messages you received")
    ap.add_argument("--like",        metavar="MSG_ID",
                    help="Find messages semantically similar to this message id "
                         "(or rowid); needs the semantic extras")
    ap.add_argument("--context",     metavar="N", type=int, default=0,
                    help="Include N messages before/after each hit (same chat)")
    ap.add_argument("--recency",     action="store_true",
                    help="Blend recency into ranking (favor newer matches)")
    ap.add_argument("--gpu",         action="store_true",
                    help="Use a GPU for embeddings if available (auto: CUDA/NVIDIA "
                         "or DirectML/any DX12 GPU); falls back to CPU. Opt-in.")
    ap.add_argument("--directml",    action="store_true",
                    help="Force the Windows DirectML GPU provider for embeddings "
                         "(AMD/NVIDIA/Intel; needs onnxruntime-directml — else CPU)")
    ap.add_argument("--verbose",     action="store_true")
    args = ap.parse_args()

    if wa_embed:
        if args.directml:
            wa_embed.enable_directml(verbose=args.verbose)
        elif args.gpu:
            wa_embed.enable_gpu(verbose=args.verbose)

    try:
        session_dir = args.session_dir or _find_session_dir()
    except RuntimeError as e:
        sys.exit(str(e))

    if args.key:
        global _cached_key
        try:
            _cached_key = bytes.fromhex(args.key)
            if len(_cached_key) != 32:
                sys.exit("--key must be 64 hex characters (256-bit AES key)")
        except ValueError:
            sys.exit("--key: invalid hex string")

    if args.contacts_key:
        global _CONTACTS_KEY_ROW2
        try:
            _CONTACTS_KEY_ROW2 = bytes.fromhex(args.contacts_key)
            if len(_CONTACTS_KEY_ROW2) != 32:
                sys.exit("--contacts-key must be 64 hex characters")
        except ValueError:
            sys.exit("--contacts-key: invalid hex string")

    if args.dump_key:
        gs_db = os.path.join(session_dir, "genericStorage.db")
        key   = find_db_key(gs_db, verbose=args.verbose)
        print(key.hex())
        return

    use_mirror = not args.no_mirror

    # `--refresh` with no query/filter is just a maintenance command — rebuild and exit.
    if args.refresh and not (args.query or args.list_chats or args.phone
                             or args.chat or args.since or args.until):
        try:
            _ensure_mirror(session_dir, force=True, verbose=args.verbose)
            print(f"Mirror rebuilt: {_MIRROR_PATH}")
        except Exception as e:
            sys.exit(f"Mirror rebuild failed: {e}")
        return

    if args.list_chats:
        chats = get_chats(session_dir=args.session_dir, use_mirror=use_mirror,
                          refresh=args.refresh, verbose=args.verbose)
        if args.json:
            sys.stdout.buffer.write(
                (json.dumps(chats, ensure_ascii=False) + "\n").encode("utf-8"))
        else:
            for c in chats:
                last = c["last"] or "?"
                print(f"{_rtl(c['display']):45s}  {c['messages']:5d} msgs  "
                      f"last {last:16s}  [{c['jid']}]")
        return

    from_me = True if args.mine else False if args.theirs else None
    if args.like:
        results = find_similar(args.like, top_k=args.top_k or 10,
                               session_dir=args.session_dir, use_mirror=use_mirror,
                               verbose=args.verbose)
        if not results and not (wa_embed and getattr(wa_embed, "EMBED_AVAILABLE", False)):
            sys.exit("--like needs the semantic extras (see README: GPU/embedding "
                     "install) and a built index")
    else:
        try:
            results = query_messages(
                query=args.query, chat=args.chat, phone=args.phone,
                since=args.since, until=args.until, top_k=args.top_k,
                mode=args.mode, from_me=from_me, context=args.context,
                recency=args.recency, session_dir=args.session_dir,
                use_mirror=use_mirror, refresh=args.refresh, verbose=args.verbose)
        except ValueError as e:
            sys.exit(f"--since / --until: {e} (use YYYY-MM-DD or e.g. 'yesterday', "
                     f"'last week', '3 days ago')")

    if args.compact:
        for r in results:
            fm = r.get("from_me")
            dir_char = ">" if fm is True else "<" if fm is False else " "
            print(f"[{r['timestamp'][:16]}] {dir_char} {_rtl((r['text'] or '').replace(chr(10), '  '))}")
        return

    if args.json:
        payload = results if results else []
        sys.stdout.buffer.write(
            (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        return

    def _dir_label(fm):
        return "sent" if fm is True else "received" if fm is False else "unknown"

    if args.csv:
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(["timestamp", "direction", "display", "chatId", "text"])
        for r in results:
            w.writerow([r["timestamp"], _dir_label(r.get("from_me")),
                        r["display"], r["chatId"], r["text"] or ""])
        return

    if args.html:
        _emit_html(results)
        return

    if not results:
        print("No results.")
        return

    print(f"Found {len(results)} result(s):\n")
    prev_chat = None
    for r in results:
        if r["chatId"] != prev_chat:
            print(f"\n── {_rtl(r['display'])}  [{r['chatId']}]")
            prev_chat = r["chatId"]
        snippet = _rtl((r["text"] or "")[:200].replace("\n", "  "))
        print(f"  [{r['timestamp']}]  {snippet}")


if __name__ == "__main__":
    main()
