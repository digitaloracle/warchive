"""Assert-based tests for wa_translit. Exits non-zero on failure.

Run:  python D:\\wa_cli\\test_wa_translit.py
"""

import sys

from wa_translit import he_to_lat, lat_to_he, expand_query

_failures = []


def check(name, cond):
    if cond:
        print(f"PASS: {name}", file=sys.stderr)
    else:
        print(f"FAIL: {name}", file=sys.stderr)
        _failures.append(name)


# --- he_to_lat -------------------------------------------------------------
# סבבה -> s b b h  => "sababa"-ish ("svvh" with our vav=v scheme actually).
check("he_to_lat sababa", he_to_lat("סבבה") == "svvh")
# שלום -> sh l v m  (final mem). shin->sh, lamed->l, vav->v, final-mem->m
check("he_to_lat shalom", he_to_lat("שלום") == "shlvm")
# leaves non-Hebrew unchanged
check("he_to_lat mixed passthrough", he_to_lat("abc 123") == "abc 123")
# final forms behave like base
check(
    "he_to_lat final forms",
    he_to_lat("ך") == "k"
    and he_to_lat("ם") == "m"
    and he_to_lat("ן") == "n"
    and he_to_lat("ף") == "f"
    and he_to_lat("ץ") == "ts",
)

# --- lat_to_he -------------------------------------------------------------
# "sababa" -> samekh alef bet alef bet alef
check("lat_to_he sababa", lat_to_he("sababa") == "סאבאבא")
# "shalom" -> shin alef lamed vav mem  (sh digraph, o->vav)
check("lat_to_he shalom", lat_to_he("shalom") == "שאלומ")
# digraph ch -> het
check("lat_to_he ch digraph", lat_to_he("ch") == "ח")
# leaves non-Latin unchanged
check("lat_to_he passthrough", lat_to_he("שלום 1") == "שלום 1")

# --- expand_query ----------------------------------------------------------
# original is always first, unchanged
heb = "סבבה"  # סבבה
ex_heb = expand_query(heb)
check("expand original first (hebrew)", ex_heb[0] == heb)
check("expand hebrew adds translit", he_to_lat(heb) in ex_heb and len(ex_heb) >= 2)

ex_lat = expand_query("shalom")
check("expand original first (latin)", ex_lat[0] == "shalom")
check("expand latin adds translit", lat_to_he("shalom") in ex_lat and len(ex_lat) >= 2)

# mixed-script input -> just [query]
mixed = "shalom שלום"
check("expand mixed returns only original", expand_query(mixed) == [mixed])

# empty input -> just [query]
check("expand empty", expand_query("") == [""])

# no-letter input -> just [query]
check("expand non-letters", expand_query("123 !!!") == ["123 !!!"])

# conservative variant: doubled letters collapsed
ex_dbl = expand_query("yallla")
check("expand collapses doubles", "yalla" in ex_dbl and ex_dbl[0] == "yallla")

# deterministic + de-duplicated across repeated calls
r1 = expand_query("sababa")
r2 = expand_query("sababa")
check("expand deterministic", r1 == r2)
check("expand de-duplicated", len(r1) == len(set(r1)))
check("expand hebrew dedup", len(ex_heb) == len(set(ex_heb)))


if _failures:
    print(f"\n{len(_failures)} FAILED: {_failures}", file=sys.stderr)
    sys.exit(1)
print("\nALL PASSED", file=sys.stderr)
sys.exit(0)
