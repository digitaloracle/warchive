# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "fastembed>=0.4",
#     "numpy",
#     "sqlite-vec",
# ]
# ///
"""Tests for wa_embed.py -- offline bilingual (Hebrew/English) semantic search.

Run:
    C:\\Users\\digit\\.local\\bin\\uv.exe run --script D:\\wa_cli\\test_wa_embed.py

Behaviour
---------
* Builds an index over ~20 synthetic bilingual sentences forming two clear
  semantic clusters (apartment/rent/lease vs food/pizza/restaurant), including
  Hebrew sentences whose English paraphrases must rank as near neighbours.
* Asserts cluster members rank ahead of unrelated ones, and that Hebrew queries
  retrieve matching English sentences (and vice versa) -- proving cross-lingual
  retrieval.
* If the model genuinely cannot be loaded/downloaded, prints a clear SKIP
  message and exits 0.
* Optional quick smoke test over the first ~500 rows of the real mirror DB.

Output goes to stdout here intentionally -- this is a standalone test runner,
not the importable module (which must stay stdout-silent).
"""

from __future__ import annotations

import os
import sys
import tempfile
import sqlite3

# Ensure we import the sibling module regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wa_embed  # noqa: E402


REAL_DB = r"D:\wa_cli\wa_mirror.db"


# ---------------------------------------------------------------------------
# Synthetic bilingual corpus
# ---------------------------------------------------------------------------
# Cluster A: housing / apartment / rent / lease
# Cluster B: food / pizza / restaurant
# Hebrew sentences are paraphrases of specific English sentences so we can prove
# cross-lingual neighbours.

CORPUS: list[tuple[int, str]] = [
    # --- Cluster A: housing (English) ---
    (1, "I am looking for a two bedroom apartment to rent downtown."),
    (2, "The monthly rent for the flat is eight thousand shekels."),
    (3, "We signed the lease agreement for the new apartment yesterday."),
    (4, "Can you send me photos of the apartment before we view it?"),
    (5, "The landlord wants a security deposit before we move in."),
    (6, "This studio is too small, I need a bigger place to live."),
    # --- Cluster A: housing (Hebrew) ---
    (7, "אני מחפש דירה של שני חדרי שינה להשכרה במרכז העיר."),   # paraphrase of (1)
    (8, "חתמנו אתמול על חוזה השכירות לדירה החדשה."),            # paraphrase of (3)
    (9, "בעל הבית רוצה פיקדון לפני שנעבור לגור."),               # paraphrase of (5)
    # --- Cluster B: food / restaurant (English) ---
    (10, "Let's order a large pepperoni pizza for dinner tonight."),
    (11, "This restaurant makes the best pasta I have ever tasted."),
    (12, "I am really hungry, can we grab some sushi for lunch?"),
    (13, "The chef recommended the grilled salmon with vegetables."),
    (14, "We booked a table for four at the Italian place at eight."),
    (15, "Do you want fries and a milkshake with your burger?"),
    # --- Cluster B: food / restaurant (Hebrew) ---
    (16, "בוא נזמין פיצה גדולה עם פפרוני לארוחת ערב הלילה."),     # paraphrase of (10)
    (17, "המסעדה הזאת מכינה את הפסטה הכי טובה שטעמתי בחיי."),     # paraphrase of (11)
    (18, "אני ממש רעב, אפשר להזמין סושי לארוחת צהריים?"),         # paraphrase of (12)
    # --- A couple of neutral distractors ---
    (19, "The weather today is sunny with a light breeze."),
    (20, "I need to charge my phone, the battery is almost dead."),
]

HOUSING_IDS = {1, 2, 3, 4, 5, 6, 7, 8, 9}
FOOD_IDS = {10, 11, 12, 13, 14, 15, 16, 17, 18}


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    raise AssertionError(msg)


def _rank_of(results, rowid: int) -> int:
    for i, (rid, _score) in enumerate(results):
        if rid == rowid:
            return i
    return 10**9


def run_tests(index_path: str) -> None:
    print(f"Backend: {'sqlite-vec' if wa_embed._use_vec() else 'numpy flat fallback'}")

    n = wa_embed.build_index(CORPUS, index_path, verbose=True)
    print(f"Indexed {n} rows; DIM={wa_embed.DIM}; index_count={wa_embed.index_count(index_path)}")
    assert n == len(CORPUS), f"expected {len(CORPUS)} indexed, got {n}"
    assert wa_embed.index_count(index_path) == len(CORPUS)
    assert wa_embed.DIM > 0

    # --- Test 1: English housing query returns housing cluster first ---
    res = wa_embed.search("Looking to rent a flat", index_path, top_k=20)
    assert res, "no results for English housing query"
    top5 = [rid for rid, _ in res[:5]]
    print(f"\n'Looking to rent a flat' -> top5 ids: {top5}")
    housing_in_top5 = sum(1 for rid in top5 if rid in HOUSING_IDS)
    if housing_in_top5 < 4:
        _fail(f"expected >=4 housing ids in top5, got {housing_in_top5} ({top5})")
    # Best result must be housing, not food.
    assert res[0][0] in HOUSING_IDS, f"top result {res[0][0]} not housing"

    # --- Test 2: English food query returns food cluster first ---
    res = wa_embed.search("Where should we eat dinner tonight?", index_path, top_k=20)
    top5 = [rid for rid, _ in res[:5]]
    print(f"'Where should we eat dinner tonight?' -> top5 ids: {top5}")
    food_in_top5 = sum(1 for rid in top5 if rid in FOOD_IDS)
    if food_in_top5 < 4:
        _fail(f"expected >=4 food ids in top5, got {food_in_top5} ({top5})")
    assert res[0][0] in FOOD_IDS, f"top result {res[0][0]} not food"

    # --- Test 3: Hebrew query retrieves matching ENGLISH sentence (cross-lingual) ---
    # Query in Hebrew about renting an apartment -> English sentence (1) should
    # rank high, and clearly ahead of any food sentence.
    he_query = "אני רוצה לשכור דירה"  # "I want to rent an apartment"
    res = wa_embed.search(he_query, index_path, top_k=20)
    top5 = [rid for rid, _ in res[:5]]
    print(f"\nHE query '{he_query}' -> top5 ids: {top5}")
    # English housing sentence (1) must appear in the top 5.
    eng_housing_top = [rid for rid in top5 if rid in {1, 2, 3, 4, 5, 6}]
    assert eng_housing_top, f"no English housing sentence in top5 for Hebrew query: {top5}"
    # The whole top5 should be housing (Heb or Eng), no food intruders.
    assert all(rid in HOUSING_IDS for rid in top5), f"food intruder in HE housing query: {top5}"
    rank_food = min(_rank_of(res, fid) for fid in FOOD_IDS)
    rank_eng1 = _rank_of(res, 1)
    assert rank_eng1 < rank_food, (
        f"English housing sent (rank {rank_eng1}) should beat best food (rank {rank_food})"
    )

    # --- Test 4: English query retrieves matching HEBREW sentence (vice versa) ---
    en_query = "I want to order a pizza"
    res = wa_embed.search(en_query, index_path, top_k=20)
    top5 = [rid for rid, _ in res[:5]]
    print(f"EN query '{en_query}' -> top5 ids: {top5}")
    # Hebrew pizza sentence (16) should be a near neighbour (top 5).
    assert 16 in top5, f"Hebrew pizza sentence (16) not in top5: {top5}"
    assert all(rid in FOOD_IDS for rid in top5), f"housing intruder in EN food query: {top5}"

    # --- Test 5: direct cross-lingual neighbour pair ---
    # Querying with the exact English of sentence (10) should surface its Hebrew
    # paraphrase (16) as a strong neighbour.
    res = wa_embed.search("Let's order a large pepperoni pizza for dinner", index_path, top_k=5)
    ids = [rid for rid, _ in res]
    print(f"Paraphrase check -> top5 ids: {ids}")
    assert 16 in ids, f"Hebrew paraphrase (16) not retrieved as neighbour: {ids}"

    # --- Test 6: blank query / empty behaviour ---
    assert wa_embed.search("", index_path) == []
    assert wa_embed.search("   ", index_path) == []
    assert wa_embed.search("anything", index_path, top_k=0) == []

    # --- Test 7: idempotent rebuild replaces index ---
    n2 = wa_embed.build_index(CORPUS[:5], index_path, verbose=False)
    assert n2 == 5, f"rebuild expected 5, got {n2}"
    assert wa_embed.index_count(index_path) == 5, "rebuild did not replace index"
    # Restore full index.
    wa_embed.build_index(CORPUS, index_path, verbose=False)

    print("\nAll synthetic bilingual semantic-search assertions PASSED.")


def smoke_real_db(index_path: str) -> None:
    """Optional quick smoke test over the first ~500 real messages."""
    if not os.path.exists(REAL_DB):
        print(f"(smoke) real DB not found at {REAL_DB}; skipping smoke test")
        return
    try:
        con = sqlite3.connect(REAL_DB)
        cur = con.execute(
            "SELECT rowid, text FROM message "
            "WHERE text IS NOT NULL AND trim(text) <> '' "
            "ORDER BY rowid LIMIT 500"
        )
        rows = [(int(r[0]), r[1]) for r in cur.fetchall()]
        con.close()
    except Exception as exc:
        print(f"(smoke) could not read real DB: {exc!r}; skipping")
        return

    n = wa_embed.build_index(rows, index_path, verbose=False)
    print(f"\n(smoke) indexed {n} real messages from first 500 rows")
    if n == 0:
        print("(smoke) nothing indexed; skipping query")
        return
    for q in ("היי מה שלומך", "ok thanks see you tomorrow"):
        res = wa_embed.search(q, index_path, top_k=3)
        print(f"(smoke) query {q!r} -> top3: {res[:3]}")
    print("(smoke) real-DB semantic query OK")


def main() -> int:
    # Probe model availability up front so we can SKIP cleanly if offline.
    model = wa_embed._get_model(verbose=True)
    if model is None or not wa_embed.EMBED_AVAILABLE:
        print(
            "SKIP: embedding model could not be loaded/downloaded in this "
            "environment (offline / no cache / missing deps). "
            f"EMBED_AVAILABLE={wa_embed.EMBED_AVAILABLE}, DIM={wa_embed.DIM}"
        )
        return 0

    print(f"EMBED_AVAILABLE={wa_embed.EMBED_AVAILABLE}, DIM={wa_embed.DIM}, model={wa_embed.MODEL_NAME}")

    with tempfile.TemporaryDirectory() as tmp:
        index_path = os.path.join(tmp, "wa_vec_test.db")
        try:
            run_tests(index_path)
        except AssertionError as exc:
            print(f"\nTEST FAILED: {exc}")
            return 1

        smoke_index = os.path.join(tmp, "wa_vec_smoke.db")
        try:
            smoke_real_db(smoke_index)
        except Exception as exc:  # smoke is best-effort, never fail the suite
            print(f"(smoke) error (non-fatal): {exc!r}")

    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
