"""Assert-based tests for wa_normalize. Plain script: exits non-zero on failure.

Run: python D:\\wa_cli\\test_wa_normalize.py
"""

import sys

from wa_normalize import (
    normalize,
    tokens,
    fold_finals,
    HEBREW_PREFIXES,
    HEBREW_SUFFIXES,
)

_failures = 0


def check(name, cond):
    global _failures
    if cond:
        print(f"PASS: {name}", file=sys.stderr)
    else:
        _failures += 1
        print(f"FAIL: {name}", file=sys.stderr)


# --- niqqud removal ---
# שָׁלוֹם (with niqqud) -> שלום (unpointed). Final mem also folds to base mem.
pointed = "שָׁלוֹם"  # שָׁלוֹם
check("niqqud removed yields unpointed shalom (final-folded)",
      normalize(pointed) == "שלומ")  # שלומ
check("no combining marks remain after normalize",
      all(0x0591 > ord(c) or ord(c) > 0x05c7 for c in normalize(pointed)))

# --- final-letter folding ---
check("fold_finals folds final mem in shalom",
      fold_finals("שלום") == "שלומ")  # שלום->שלומ
check("fold_finals folds all five finals",
      fold_finals("ךםןףץ") == "כמנפצ")
check("normalize folds final letter",
      normalize("שלום").endswith("מ"))

# --- bidi-control removal ---
bidi = "abc‎‫⁦def⁩"
check("bidi controls removed", normalize(bidi) == "abcdef")
check("bidi removal across all controls",
      normalize("‎‏‪‫‬‭‮⁦⁧⁨⁩x") == "x")

# --- idempotency on mixed Hebrew/English/emoji ---
mixed = "  Pizza שָׁלום \U0001f355 Don't ‎GO  "
once = normalize(mixed)
check("idempotent on mixed string", normalize(once) == once)
check("idempotent generic", normalize(normalize(bidi)) == normalize(bidi))

# --- casefolding Latin while Hebrew unchanged ---
check("Pizza casefolds to pizza", normalize("Pizza") == "pizza")
heb = "שלומ"  # שלומ (already base form)
check("Hebrew unchanged by casefold", normalize(heb) == heb)

# --- apostrophe preserved in Latin words ---
check("don't keeps apostrophe", normalize("Don't") == "don't")
check("don't tokenizes keeping apostrophe-free word", "don't" in normalize("I Don't know"))

# --- geresh / gershayim stripped, ascii quotes kept ---
check("geresh stripped", normalize("צ׳") == "צ")  # צ׳ -> צ
check("gershayim stripped", normalize("ר״ת") == "רת")

# --- prefix + suffix stemming ---
# הספרים = ה (prefix) + ספר (stem) + ים (suffix) -> ספר ; original also returned.
# Note: final mem in the suffix folds during normalize, so the returned
# "original" token is the normalized form הספרימ (folded final mem).
word = "הספרים"  # הספרים
toks = tokens(word)
stem = "ספר"          # ספר
orig_norm = "הספרימ"  # normalized (final-folded) form of הספרים
check("stem strips prefix+suffix to root",
      stem in toks)
check("original (normalized) token also returned alongside stem",
      orig_norm in toks)
check("stem comes before original",
      toks.index(stem) < toks.index(orig_norm))

# --- tokens drops short tokens ---
check("tokens drops <2 char tokens", tokens("a be") == ["be"])

# --- tokens dedup when stem == original ---
check("no duplicate when stem equals original",
      tokens("pizza") == ["pizza"])

# --- public API surface ---
check("HEBREW_PREFIXES is tuple", isinstance(HEBREW_PREFIXES, tuple))
check("HEBREW_SUFFIXES is tuple", isinstance(HEBREW_SUFFIXES, tuple))
check("prefixes match spec",
      HEBREW_PREFIXES == ('וה', 'שה', 'בה', 'כה',
                          'לה', 'מה', 'ב', 'כ', 'ל',
                          'ש', 'ה', 'ו', 'מ'))

# --- stem guard: don't reduce below 2 chars ---
# שה is a prefix but stripping from a 3-char word that would leave <2 must be guarded.
check("prefix not stripped if stem would be <2",
      "הי" in tokens("הי"))  # הי stays (2 chars, prefix ה would leave 1)

if _failures:
    print(f"\n{_failures} test(s) FAILED", file=sys.stderr)
    sys.exit(1)
print("\nALL TESTS PASSED", file=sys.stderr)
sys.exit(0)
