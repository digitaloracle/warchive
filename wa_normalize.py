"""Text normalization and tokenization for bilingual Hebrew/English search.

Stdlib-only (unicodedata, re). Used by the search layer at both index time
and query time. read-only, no side effects.
"""

import re
import unicodedata

__all__ = [
    "normalize",
    "tokens",
    "fold_finals",
    "HEBREW_PREFIXES",
    "HEBREW_SUFFIXES",
]

# --- Hebrew prefixes / suffixes (ordered, longest-first within tiers) ---
HEBREW_PREFIXES = ('וה', 'שה', 'בה', 'כה', 'לה', 'מה', 'ב', 'כ', 'ל', 'ש', 'ה', 'ו', 'מ')
HEBREW_SUFFIXES = ('יהם', 'ים', 'ות', 'כם', 'נו', 'תי', 'תם', 'ך', 'ו', 'ה')

# --- Niqqud / points / cantillation to strip (Hebrew combining marks) ---
# U+0591..U+05BD plus a few explicit extras.
_NIQQUD = set(range(0x0591, 0x05BD + 1)) | {0x05BF, 0x05C1, 0x05C2, 0x05C4, 0x05C5, 0x05C7}

# --- Bidirectional control characters (have caused crashes elsewhere) ---
_BIDI = {
    0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x2066, 0x2067, 0x2068, 0x2069,
}

# --- Geresh / gershayim used in Hebrew acronyms / abbreviations ---
_HEB_PUNCT = {0x05F3, 0x05F4}

# Code points removed entirely during normalization.
_REMOVE = _NIQQUD | _BIDI | _HEB_PUNCT

# --- Hebrew final-form -> base-form folding ---
_FINALS = {
    'ך': 'כ',
    'ם': 'מ',
    'ן': 'נ',
    'ף': 'פ',
    'ץ': 'צ',
}
_FINALS_TRANS = str.maketrans(_FINALS)

# Stemming runs on normalized tokens, where Hebrew final letters have already
# been folded to base forms. Several affixes contain letters that fold when
# final (ם, ך). Match against folded affix forms so they still apply, while the
# public HEBREW_PREFIXES / HEBREW_SUFFIXES tuples stay exactly as specified.
_PREFIXES_FOLDED = tuple(p.translate(_FINALS_TRANS) for p in HEBREW_PREFIXES)
_SUFFIXES_FOLDED = tuple(s.translate(_FINALS_TRANS) for s in HEBREW_SUFFIXES)

# Token pattern: spans of Hebrew letters, Latin letters and digits.
_TOKEN_RE = re.compile(r"[֐-׿\w]+")

# Whitespace collapse.
_WS_RE = re.compile(r"\s+")


def fold_finals(s: str) -> str:
    """Fold Hebrew final-form letters to their base form (ך->כ, etc.)."""
    return s.translate(_FINALS_TRANS)


def normalize(text: str) -> str:
    """Normalize bilingual Hebrew/English text for search.

    NFC normalize; strip niqqud/cantillation, bidi controls, and Hebrew
    geresh/gershayim; fold Hebrew final letters; casefold; collapse
    whitespace. Idempotent.
    """
    if not text:
        return ""

    # Unicode NFC.
    text = unicodedata.normalize("NFC", text)

    # Strip removable code points (niqqud, bidi controls, geresh/gershayim).
    text = "".join(ch for ch in text if ord(ch) not in _REMOVE)

    # Fold Hebrew final-form letters.
    text = fold_finals(text)

    # Casefold (leaves Hebrew unchanged, lowercases Latin, keeps don't intact).
    text = text.casefold()

    # Collapse whitespace and strip.
    text = _WS_RE.sub(" ", text).strip()

    return text


def _stem(token: str) -> str:
    """Strip one Hebrew prefix then one Hebrew suffix (longest-first),
    each only if the remaining stem stays >= 2 chars."""
    stem = token

    for prefix in _PREFIXES_FOLDED:
        if stem.startswith(prefix) and len(stem) - len(prefix) >= 2:
            stem = stem[len(prefix):]
            break

    for suffix in _SUFFIXES_FOLDED:
        if stem.endswith(suffix) and len(stem) - len(suffix) >= 2:
            stem = stem[:len(stem) - len(suffix)]
            break

    return stem


def tokens(text: str) -> list[str]:
    """Normalize, tokenize, and morphologically stem.

    Returns both the stemmed form and the original token when they differ,
    deduplicated within a token but preserving overall order.
    """
    out: list[str] = []
    seen: set[str] = set()

    for tok in _TOKEN_RE.findall(normalize(text)):
        if len(tok) < 2:
            continue

        stem = _stem(tok)

        for candidate in (stem, tok):  # stemmed form first, then original
            if candidate not in seen and len(candidate) >= 2:
                seen.add(candidate)
                out.append(candidate)

    return out
