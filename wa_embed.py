# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "fastembed>=0.4",
#     "numpy",
#     "sqlite-vec",
# ]
# ///
"""Local, offline semantic (vector) search for the warchive WhatsApp mirror.

This module provides cross-lingual (Hebrew <-> English) semantic search over the
plaintext message mirror, complementing keyword search by catching paraphrases
and matches across languages.

Design notes / constraints
---------------------------
* Embedding model: ``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2``
  served via ``fastembed`` (ONNX runtime, no torch). 384 dimensions, ~0.22 GB
  download, fully offline after the first download. It is multilingual and
  supports Hebrew. This model does NOT need E5-style "query:"/"passage:"
  prefixes, so none are applied.
* Vector store: ``sqlite-vec`` (a vec0 virtual table keyed by rowid). If the
  extension cannot be loaded, we transparently fall back to a flat numpy index
  persisted as ``<index_path>.npy`` (vectors) + ``<index_path>.ids.npy`` (ids),
  keeping the exact same public API. Cosine similarity is used throughout
  (vectors are L2-normalised, so cosine == dot product).
* NEVER writes to stdout. All optional logging goes to stderr and only when
  ``verbose=True``. This is important: the module is imported inside an MCP
  stdio server where stdout is the protocol channel.
* The model is lazy-loaded: importing this module is cheap and never crashes if
  the model / dependencies are absent. Availability is probed lazily; importers
  can read ``EMBED_AVAILABLE`` / ``DIM`` (they are resolved on first access of
  the public functions, and best-effort at import time).

Public API
----------
* ``EMBED_AVAILABLE: bool`` -- True only if model + storage are usable.
* ``DIM: int`` -- embedding dimension (0 if unavailable).
* ``build_index(rows, index_path, *, verbose=False) -> int``
* ``search(query, index_path, top_k=200) -> list[tuple[int, float]]``
* ``index_count(index_path) -> int``
"""

from __future__ import annotations

import os
import sys
import struct
import sqlite3
from typing import Iterable

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
# Known embedding dimension for the model above. Used as a cheap default so
# importers can read DIM without forcing a model download. Verified at build
# time against the real model output.
_EXPECTED_DIM = 384

# E5-family models require "query: " / "passage: " prefixes. The MiniLM
# paraphrase model does NOT, so these are empty. Kept here so a future swap to
# an E5 model only needs these two constants changed.
_QUERY_PREFIX = ""
_PASSAGE_PREFIX = ""

_BATCH_SIZE = 256

# ONNX Runtime execution providers to request when loading the model. None =
# fastembed default (CPU). Set via use_providers()/enable_gpu()/enable_directml()
# BEFORE the model is first loaded (it is cached). Loading always falls back to CPU if
# a requested GPU provider is unavailable, so this never hard-fails.
_PROVIDERS: "list[str] | None" = None

# Public availability flag / dimension. These are best-effort at import time
# (cheap: we only check that the dependencies import) and are refined the first
# time the model is actually loaded.
EMBED_AVAILABLE: bool = False
DIM: int = 0

# ---------------------------------------------------------------------------
# Logging (stderr only, never stdout)
# ---------------------------------------------------------------------------


def _log(verbose: bool, msg: str) -> None:
    if verbose:
        print(f"[wa_embed] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Lazy dependency / model loading
# ---------------------------------------------------------------------------

_model = None  # cached fastembed TextEmbedding instance
_model_load_failed = False
_numpy = None
_sqlite_vec_mod = None
_sqlite_vec_checked = False


def _np():
    """Return the numpy module, importing lazily. Raises if unavailable."""
    global _numpy
    if _numpy is None:
        import numpy as np  # noqa: PLC0415

        _numpy = np
    return _numpy


def _probe_import_available() -> bool:
    """Cheap check: can we import the core dependencies at all?

    Does NOT download the model. Used to give a best-effort value for
    EMBED_AVAILABLE at import time.
    """
    try:
        import importlib.util

        for name in ("fastembed", "numpy"):
            if importlib.util.find_spec(name) is None:
                return False
        return True
    except Exception:
        return False


def _get_model(verbose: bool = False):
    """Lazily construct and cache the embedding model.

    Returns the model instance, or None if it cannot be loaded (e.g. the model
    cannot be downloaded in an offline environment with no cache). Updates the
    module-level EMBED_AVAILABLE / DIM globals as a side effect.
    """
    global _model, _model_load_failed, EMBED_AVAILABLE, DIM
    if _model is not None:
        return _model
    if _model_load_failed:
        return None
    try:
        from fastembed import TextEmbedding  # noqa: PLC0415

        _log(verbose, f"loading model {MODEL_NAME} (first run may download ~0.22 GB)")
        try:
            _model = TextEmbedding(
                model_name=MODEL_NAME,
                **({"providers": _PROVIDERS} if _PROVIDERS else {}),
            )
            if _PROVIDERS:
                _log(verbose, f"requested execution providers: {_PROVIDERS}")
        except Exception as prov_exc:
            if not _PROVIDERS:
                raise
            _log(verbose, f"GPU providers {_PROVIDERS} failed ({prov_exc!r}); "
                          f"falling back to CPU")
            _model = TextEmbedding(model_name=MODEL_NAME)
        # Confirm dimension from a tiny probe so DIM reflects reality.
        try:
            vec = next(iter(_model.embed(["dimension probe"])))
            DIM = int(len(vec))
        except Exception:
            DIM = _EXPECTED_DIM
        EMBED_AVAILABLE = True
        _log(verbose, f"model ready, DIM={DIM}")
        return _model
    except Exception as exc:  # network down, no cache, missing dep, etc.
        _model_load_failed = True
        EMBED_AVAILABLE = False
        _log(verbose, f"model load failed: {exc!r}")
        return None


def available_providers() -> list:
    """ONNX Runtime execution providers compiled into the installed onnxruntime."""
    try:
        import onnxruntime as ort  # noqa: PLC0415
        return list(ort.get_available_providers())
    except Exception:
        return []


def use_providers(providers) -> None:
    """Set the ORT execution providers used when the model loads (call before
    first embed). Pass None/empty to reset to the default (CPU)."""
    global _PROVIDERS
    _PROVIDERS = list(providers) if providers else None


def enable_directml(verbose: bool = False) -> bool:
    """Request the Windows DirectML execution provider — runs on any DX12 GPU
    (AMD, NVIDIA, or Intel). Returns True if available, else False (stays on CPU).
    Requires onnxruntime-directml installed in place of the stock CPU onnxruntime;
    the CPU onnxruntime won't expose it."""
    avail = available_providers()
    if "DmlExecutionProvider" in avail:
        use_providers(["DmlExecutionProvider", "CPUExecutionProvider"])
        _log(verbose, "GPU enabled via DmlExecutionProvider (DirectML)")
        return True
    _log(verbose, "DirectML not available in this onnxruntime build; using CPU "
                  "(pip install onnxruntime-directml to enable GPU on Windows)")
    return False


def enable_gpu(verbose: bool = False):
    """Vendor-agnostic GPU opt-in: pick the best available execution provider
    (CUDA for NVIDIA, then DirectML for any DX12 GPU), else stay on CPU. Returns
    the chosen provider name or None. Safe everywhere — needs a GPU-enabled
    onnxruntime (onnxruntime-gpu for CUDA / onnxruntime-directml for DirectML);
    otherwise it just uses CPU."""
    avail = available_providers()
    for prov in ("CUDAExecutionProvider", "DmlExecutionProvider"):
        if prov in avail:
            use_providers([prov, "CPUExecutionProvider"])
            _log(verbose, f"GPU enabled via {prov}")
            return prov
    _log(verbose, "no GPU execution provider available; using CPU "
                  "(install onnxruntime-gpu for NVIDIA, or onnxruntime-directml "
                  "for DirectML/AMD on Windows)")
    return None


def _embed_texts(texts: list[str], *, is_query: bool, verbose: bool = False):
    """Embed a list of texts into L2-normalised float32 vectors (numpy 2D array)."""
    model = _get_model(verbose=verbose)
    if model is None:
        raise RuntimeError("embedding model unavailable")
    np = _np()
    prefix = _QUERY_PREFIX if is_query else _PASSAGE_PREFIX
    if prefix:
        texts = [prefix + t for t in texts]
    vecs = np.asarray(list(model.embed(texts, batch_size=_BATCH_SIZE)), dtype=np.float32)
    if vecs.ndim == 1:
        vecs = vecs.reshape(1, -1)
    # L2-normalise so dot product == cosine similarity. Guard zero-norm.
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs = vecs / norms
    return vecs


# ---------------------------------------------------------------------------
# sqlite-vec backend detection
# ---------------------------------------------------------------------------


def _sqlite_vec():
    """Return the sqlite_vec module if it is importable AND loadable, else None.

    Result is cached. A failure here triggers the numpy flat-index fallback.
    """
    global _sqlite_vec_mod, _sqlite_vec_checked
    if _sqlite_vec_checked:
        return _sqlite_vec_mod
    _sqlite_vec_checked = True
    try:
        import sqlite_vec  # noqa: PLC0415

        # Verify the extension actually loads on this interpreter build.
        db = sqlite3.connect(":memory:")
        try:
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.execute("SELECT vec_version()").fetchone()
        finally:
            db.close()
        _sqlite_vec_mod = sqlite_vec
    except Exception:
        _sqlite_vec_mod = None
    return _sqlite_vec_mod


def _use_vec() -> bool:
    return _sqlite_vec() is not None


# ---------------------------------------------------------------------------
# numpy flat-index fallback paths
# ---------------------------------------------------------------------------


def _npy_paths(index_path: str) -> tuple[str, str, str]:
    """Return (vectors_path, ids_path, meta_path) for the flat numpy fallback."""
    vec_path = index_path + ".npy"
    ids_path = index_path + ".ids.npy"
    meta_path = index_path + ".meta"
    return vec_path, ids_path, meta_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_index(rows: Iterable[tuple[int, str]], index_path: str, *, verbose: bool = False) -> int:
    """Build (or rebuild) the semantic index.

    Parameters
    ----------
    rows : iterable of (rowid:int, text:str)
        Source rows. Empty / whitespace-only texts are skipped.
    index_path : str
        Path of the index. For the sqlite-vec backend this is the sqlite db
        file; for the numpy fallback, sibling ``.npy`` files are written.
    verbose : bool, keyword-only
        If True, emit progress to stderr (never stdout).

    Returns
    -------
    int
        Number of rows indexed.

    The operation is idempotent: a rebuild replaces any existing index.
    """
    # Gather + de-dupe-by-rowid (last wins) and skip empties.
    items: list[tuple[int, str]] = []
    seen: dict[int, int] = {}
    for rowid, text in rows:
        if text is None:
            continue
        if not str(text).strip():
            continue
        rid = int(rowid)
        if rid in seen:
            items[seen[rid]] = (rid, str(text))
        else:
            seen[rid] = len(items)
            items.append((rid, str(text)))

    if not items:
        _log(verbose, "no non-empty rows to index")
        # Still (re)create an empty index so index_count reflects reality.
        _write_empty_index(index_path, verbose=verbose)
        return 0

    model = _get_model(verbose=verbose)
    if model is None:
        _log(verbose, "model unavailable; cannot build index")
        return 0

    np = _np()
    ids = np.asarray([rid for rid, _ in items], dtype=np.int64)
    texts = [t for _, t in items]

    _log(verbose, f"embedding {len(texts)} texts in batches of {_BATCH_SIZE}")
    vecs = _embed_texts(texts, is_query=False, verbose=verbose)
    dim = int(vecs.shape[1])

    if _use_vec():
        _build_index_vec(ids, vecs, dim, index_path, verbose=verbose)
    else:
        _build_index_npy(ids, vecs, dim, index_path, verbose=verbose)

    _log(verbose, f"indexed {len(ids)} rows (backend={'sqlite-vec' if _use_vec() else 'numpy'})")
    return int(len(ids))


def _write_empty_index(index_path: str, *, verbose: bool = False) -> None:
    """Create an empty index (used when there is nothing to embed)."""
    if _use_vec():
        # Without a known dimension we cannot create the vec0 table. Fall back to
        # removing any stale index so index_count() returns 0.
        for p in (index_path,):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
    else:
        vec_path, ids_path, meta_path = _npy_paths(index_path)
        for p in (vec_path, ids_path, meta_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def _build_index_vec(ids, vecs, dim: int, index_path: str, *, verbose: bool = False) -> None:
    sqlite_vec = _sqlite_vec()
    # Fresh build: remove any existing db file for a clean, idempotent rebuild.
    try:
        if os.path.exists(index_path):
            os.remove(index_path)
    except OSError:
        pass

    parent = os.path.dirname(os.path.abspath(index_path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    db = sqlite3.connect(index_path)
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        # Store the dimension so search() can validate without a model.
        db.execute("CREATE TABLE IF NOT EXISTS wa_meta(key TEXT PRIMARY KEY, value TEXT)")
        db.execute(
            "INSERT OR REPLACE INTO wa_meta(key, value) VALUES('dim', ?)", (str(dim),)
        )
        db.execute(
            "INSERT OR REPLACE INTO wa_meta(key, value) VALUES('model', ?)", (MODEL_NAME,)
        )
        # vec0 virtual table keyed by message rowid.
        db.execute(
            f"CREATE VIRTUAL TABLE vec_items USING vec0("
            f"rowid INTEGER PRIMARY KEY, embedding FLOAT[{dim}])"
        )
        rows = (
            (int(rid), _serialize_f32(vec))
            for rid, vec in zip(ids.tolist(), vecs)
        )
        db.executemany(
            "INSERT INTO vec_items(rowid, embedding) VALUES (?, ?)", rows
        )
        db.commit()
    finally:
        db.close()


def _build_index_npy(ids, vecs, dim: int, index_path: str, *, verbose: bool = False) -> None:
    np = _np()
    vec_path, ids_path, meta_path = _npy_paths(index_path)
    parent = os.path.dirname(os.path.abspath(vec_path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    np.save(vec_path, vecs.astype(np.float32))
    np.save(ids_path, ids.astype(np.int64))
    with open(meta_path, "w", encoding="utf-8") as fh:
        fh.write(f"dim={dim}\nmodel={MODEL_NAME}\n")


def _serialize_f32(vec) -> bytes:
    """Pack a 1D float vector into the little-endian float32 blob sqlite-vec wants."""
    return struct.pack(f"<{len(vec)}f", *(float(x) for x in vec))


def search(query: str, index_path: str, top_k: int = 200) -> list[tuple[int, float]]:
    """Semantic search. Returns ``[(rowid, similarity)]`` best-first.

    Returns ``[]`` if the model/storage is unavailable, the index is missing or
    empty, or the query is blank.
    """
    if query is None or not str(query).strip():
        return []
    if top_k <= 0:
        return []

    # Distinguish "no index" from "model unavailable": both yield [].
    if _use_vec():
        if not os.path.exists(index_path):
            return []
    else:
        vec_path, ids_path, _ = _npy_paths(index_path)
        if not (os.path.exists(vec_path) and os.path.exists(ids_path)):
            return []

    model = _get_model()
    if model is None:
        return []

    try:
        qvec = _embed_texts([str(query)], is_query=True)[0]
    except Exception:
        return []

    if _use_vec():
        return _search_vec(qvec, index_path, top_k)
    return _search_npy(qvec, index_path, top_k)


def _search_vec(qvec, index_path: str, top_k: int) -> list[tuple[int, float]]:
    sqlite_vec = _sqlite_vec()
    db = sqlite3.connect(index_path)
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        # KNN query. sqlite-vec returns L2 distance for FLOAT[]; because vectors
        # are L2-normalised, cosine_sim = 1 - (dist^2)/2. We convert so the
        # public score is a cosine similarity in [-1, 1], best-first.
        blob = _serialize_f32(qvec)
        cur = db.execute(
            "SELECT rowid, distance FROM vec_items "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (blob, int(top_k)),
        )
        out: list[tuple[int, float]] = []
        for rid, dist in cur.fetchall():
            cos = 1.0 - (float(dist) * float(dist)) / 2.0
            out.append((int(rid), cos))
        # Already ordered by ascending distance == descending cosine.
        return out
    except Exception:
        return []
    finally:
        db.close()


def _search_npy(qvec, index_path: str, top_k: int) -> list[tuple[int, float]]:
    np = _np()
    vec_path, ids_path, _ = _npy_paths(index_path)
    try:
        vecs = np.load(vec_path)
        ids = np.load(ids_path)
    except Exception:
        return []
    if vecs.size == 0 or ids.size == 0:
        return []
    # Vectors and query are L2-normalised, so dot == cosine.
    sims = vecs @ qvec.astype(vecs.dtype)
    k = min(int(top_k), sims.shape[0])
    if k <= 0:
        return []
    # Partial top-k then sort that slice descending.
    idx = np.argpartition(-sims, k - 1)[:k]
    idx = idx[np.argsort(-sims[idx])]
    return [(int(ids[i]), float(sims[i])) for i in idx]


def index_count(index_path: str) -> int:
    """Return the number of vectors stored in the index (0 if none/missing)."""
    if _use_vec():
        if not os.path.exists(index_path):
            return 0
        sqlite_vec = _sqlite_vec()
        db = sqlite3.connect(index_path)
        try:
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            row = db.execute("SELECT count(*) FROM vec_items").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0
        finally:
            db.close()
    else:
        vec_path, ids_path, _ = _npy_paths(index_path)
        if not os.path.exists(ids_path):
            return 0
        try:
            np = _np()
            ids = np.load(ids_path)
            return int(ids.shape[0])
        except Exception:
            return 0


def indexed_ids(index_path: str) -> set:
    """Return the set of rowids currently present in the index (empty if none)."""
    if _use_vec():
        if not os.path.exists(index_path):
            return set()
        sqlite_vec = _sqlite_vec()
        db = sqlite3.connect(index_path)
        try:
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            return {int(r[0]) for r in db.execute("SELECT rowid FROM vec_items")}
        except Exception:
            return set()
        finally:
            db.close()
    else:
        _, ids_path, _ = _npy_paths(index_path)
        if not os.path.exists(ids_path):
            return set()
        try:
            return {int(x) for x in _np().load(ids_path).tolist()}
        except Exception:
            return set()


def add_index(rows: Iterable[tuple[int, str]], index_path: str, *,
              verbose: bool = False) -> int:
    """Incrementally embed and append ONLY rows whose rowid is not already indexed.

    `rows` may be the full corpus — already-indexed rowids and empty texts are
    skipped, so only new messages are embedded. Falls back to a full build when
    no index exists yet. Returns the number of NEW rows added."""
    existing = indexed_ids(index_path)
    items: list[tuple[int, str]] = []
    seen: set = set()
    for rowid, text in rows:
        if text is None or not str(text).strip():
            continue
        rid = int(rowid)
        if rid in existing or rid in seen:
            continue
        seen.add(rid)
        items.append((rid, str(text)))

    if not existing:                      # nothing indexed yet -> normal build
        return build_index(items, index_path, verbose=verbose)
    if not items:
        _log(verbose, "incremental: no new rows to embed")
        return 0

    model = _get_model(verbose=verbose)
    if model is None:
        return 0
    np = _np()
    ids = np.asarray([rid for rid, _ in items], dtype=np.int64)
    texts = [t for _, t in items]
    _log(verbose, f"incremental: embedding {len(texts)} new texts")
    vecs = _embed_texts(texts, is_query=False, verbose=verbose)
    dim = int(vecs.shape[1])
    if _use_vec():
        _append_index_vec(ids, vecs, dim, index_path, verbose=verbose)
    else:
        _append_index_npy(ids, vecs, dim, index_path, verbose=verbose)
    return int(len(ids))


def _append_index_vec(ids, vecs, dim: int, index_path: str, *, verbose: bool = False) -> None:
    sqlite_vec = _sqlite_vec()
    db = sqlite3.connect(index_path)
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        row = db.execute("SELECT value FROM wa_meta WHERE key='dim'").fetchone()
        if row and int(row[0]) != dim:
            raise ValueError(f"embedding dim mismatch: index {row[0]} != model {dim}")
        db.executemany(
            "INSERT OR REPLACE INTO vec_items(rowid, embedding) VALUES (?, ?)",
            ((int(rid), _serialize_f32(vec)) for rid, vec in zip(ids.tolist(), vecs)),
        )
        db.commit()
    finally:
        db.close()


def _append_index_npy(ids, vecs, dim: int, index_path: str, *, verbose: bool = False) -> None:
    np = _np()
    vec_path, ids_path, _ = _npy_paths(index_path)
    old_v = np.load(vec_path)
    old_i = np.load(ids_path)
    np.save(vec_path, np.vstack([old_v, vecs.astype(old_v.dtype)]))
    np.save(ids_path, np.concatenate([old_i, ids.astype(old_i.dtype)]))


# ---------------------------------------------------------------------------
# Import-time best-effort availability probe (cheap; no model download)
# ---------------------------------------------------------------------------

EMBED_AVAILABLE = _probe_import_available()
DIM = _EXPECTED_DIM if EMBED_AVAILABLE else 0
