"""Bilingual Hebrew<->Latin transliteration and query expansion for warchive.

Standard library only (re, unicodedata). Deterministic, read-only helpers used
to widen search recall over bilingual (Hebrew + English) WhatsApp chat history
where Israeli users heavily code-switch and transliterate.

Public API
----------
he_to_lat(s: str) -> str
lat_to_he(s: str) -> str
expand_query(query: str) -> list[str]
"""

import re
import unicodedata

__all__ = ["he_to_lat", "lat_to_he", "expand_query"]

# ---------------------------------------------------------------------------
# Hebrew -> Latin
# ---------------------------------------------------------------------------
# Final forms map to the same value as their base letter.
_HE_TO_LAT = {
    "א": "",    # alef     -> ''
    "ב": "v",   # bet      -> v
    "ג": "g",   # gimel    -> g
    "ד": "d",   # dalet    -> d
    "ה": "h",   # he       -> h
    "ו": "v",   # vav      -> v
    "ז": "z",   # zayin    -> z
    "ח": "ch",  # het      -> ch
    "ט": "t",   # tet      -> t
    "י": "y",   # yod      -> y
    "ך": "k",   # final kaf
    "כ": "k",   # kaf      -> k
    "ל": "l",   # lamed    -> l
    "ם": "m",   # final mem
    "מ": "m",   # mem      -> m
    "ן": "n",   # final nun
    "נ": "n",   # nun      -> n
    "ס": "s",   # samekh   -> s
    "ע": "",    # ayin     -> ''
    "ף": "f",   # final pe
    "פ": "f",   # pe       -> f
    "ץ": "ts",  # final tsadi
    "צ": "ts",  # tsadi    -> ts
    "ק": "k",   # qof      -> k
    "ר": "r",   # resh     -> r
    "ש": "sh",  # shin     -> sh
    "ת": "t",   # tav      -> t
}


def he_to_lat(s: str) -> str:
    """Rule-based Hebrew -> Latin transliteration.

    Each Hebrew letter is mapped to a reasonable Latin equivalent (final forms
    treated as their base form). Hebrew niqqud / cantillation marks are dropped.
    Non-Hebrew characters are left unchanged.
    """
    if not s:
        return s
    out = []
    for ch in s:
        if ch in _HE_TO_LAT:
            out.append(_HE_TO_LAT[ch])
            continue
        # Drop Hebrew combining marks (niqqud, dagesh, cantillation): U+0591..U+05C7
        if "֑" <= ch <= "ׇ":
            continue
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Latin -> Hebrew  (best-effort; inherently ambiguous, one candidate)
# ---------------------------------------------------------------------------
# Digraphs handled before single letters (longest-match first).
_LAT_DIGRAPHS = [
    ("sh", "ש"),  # sh -> shin
    ("ch", "ח"),  # ch -> het
    ("kh", "ח"),  # kh -> het
    ("ts", "צ"),  # ts -> tsadi
    ("tz", "צ"),  # tz -> tsadi
    ("ph", "פ"),  # ph -> pe
    ("th", "ת"),  # th -> tav
]

_LAT_SINGLE = {
    "a": "א",  # a -> alef
    "b": "ב",  # b -> bet
    "c": "ק",  # c -> qof  (hard-c)
    "d": "ד",  # d -> dalet
    "e": "א",  # e -> alef
    "f": "פ",  # f -> pe
    "g": "ג",  # g -> gimel
    "h": "ה",  # h -> he
    "i": "י",  # i -> yod
    "j": "ג",  # j -> gimel
    "k": "ק",  # k -> qof
    "l": "ל",  # l -> lamed
    "m": "מ",  # m -> mem
    "n": "נ",  # n -> nun
    "o": "ו",  # o -> vav
    "p": "פ",  # p -> pe
    "q": "ק",  # q -> qof
    "r": "ר",  # r -> resh
    "s": "ס",  # s -> samekh
    "t": "ת",  # t -> tav
    "u": "ו",  # u -> vav
    "v": "ו",  # v -> vav
    "w": "ו",  # w -> vav
    "x": "קס",  # x -> qof+samekh (ks)
    "y": "י",  # y -> yod
    "z": "ז",  # z -> zayin
}


def lat_to_he(s: str) -> str:
    """Best-effort Latin -> Hebrew transliteration.

    Produces one reasonable candidate. Common digraphs (sh/ch/ts/tz/...) are
    matched before single letters. Non-Latin characters are left unchanged.
    """
    if not s:
        return s
    out = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        lower = ch.lower()
        if "a" <= lower <= "z":
            pair = s[i:i + 2].lower()
            matched = False
            for dg, heb in _LAT_DIGRAPHS:
                if pair == dg:
                    out.append(heb)
                    i += 2
                    matched = True
                    break
            if matched:
                continue
            out.append(_LAT_SINGLE.get(lower, ch))
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Query expansion
# ---------------------------------------------------------------------------
_HEBREW_LETTER_RE = re.compile(r"[א-ת]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
# A "pure" script run: only letters of that script plus separators
# (spaces / hyphens / apostrophes). No letters of the other script.
_PURE_HEBREW_RE = re.compile(r"^[א-ת\s'\-]+$")
_PURE_LATIN_RE = re.compile(r"^[A-Za-z\s'\-]+$")

_DOUBLE_LETTER_RE = re.compile(r"([A-Za-z])\1")


def _collapse_doubles(s: str) -> str:
    """Collapse runs of a doubled Latin letter to a single one (e.g. yallla)."""
    return _DOUBLE_LETTER_RE.sub(r"\1", s)


def _add_unique(seq: list, value) -> None:
    if value and value not in seq:
        seq.append(value)


def expand_query(query: str) -> list[str]:
    """Expand a search query to widen bilingual recall.

    Returns a de-duplicated list whose FIRST element is always the original
    query, unchanged. For a purely-Hebrew query the Latin transliteration is
    appended; for a purely-Latin query the Hebrew transliteration is appended.
    A small number of conservative spelling variants (collapsing doubled
    letters) may also be added. Mixed-script, empty, or non-letter input
    yields just ``[query]``.
    """
    result = [query]

    if not query:
        return result

    has_hebrew = bool(_HEBREW_LETTER_RE.search(query))
    has_latin = bool(_LATIN_LETTER_RE.search(query))

    if has_hebrew and not has_latin and _PURE_HEBREW_RE.match(query):
        _add_unique(result, he_to_lat(query))
    elif has_latin and not has_hebrew and _PURE_LATIN_RE.match(query):
        translit = lat_to_he(query)
        _add_unique(result, translit)
        # Conservative variant: collapse doubled letters (e.g. "yallla"->"yalla")
        collapsed = _collapse_doubles(query)
        if collapsed != query:
            _add_unique(result, collapsed)
            _add_unique(result, lat_to_he(collapsed))
    # else: mixed-script / no letters -> just [query]

    return result
