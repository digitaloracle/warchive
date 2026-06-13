# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "cryptography", "python-bidi", "python-snappy",
#     "fastembed", "sqlite-vec", "numpy",
# ]
# ///
"""Integration test for hybrid retrieval (FTS5 + semantic + fusion).

Runs in a uv env that HAS the embedding deps, so it exercises the full path
including the one-time vector-index build. Requires a built wa_mirror.db.
Run: uv run --script test_retrieval.py
"""
import sys

import wa_search

fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}",
          file=sys.stderr)
    if not cond:
        fails.append(name)


# Embeddings must be available in this uv env.
check("embeddings available in uv env",
      bool(wa_search.wa_embed and wa_search.wa_embed.EMBED_AVAILABLE),
      f"EMBED_AVAILABLE={getattr(wa_search.wa_embed, 'EMBED_AVAILABLE', None)}")

# Lexical mode (FTS5) returns hits for a common token.
lex = wa_search.query_messages(query="pizza", mode="lexical", top_k=10)
check("lexical returns hits", len(lex) > 0, f"{len(lex)} hits")
check("lexical results carry a snippet", any(r.get("snippet") for r in lex))

# Hebrew morphological match (stemmed FTS) — inflected/prefixed forms.
heb = wa_search.query_messages(query="פיצה", mode="lexical", top_k=10)
check("hebrew lexical returns hits (stemming)", len(heb) > 0, f"{len(heb)} hits")

# Semantic mode — triggers the one-time vector index build, then ranks by meaning.
sem = wa_search.query_messages(query="renting an apartment", mode="semantic", top_k=10)
check("semantic returns hits", len(sem) > 0, f"{len(sem)} hits")
check("vector index was built", wa_search.wa_embed.index_count(wa_search._VEC_PATH) > 0,
      f"{wa_search.wa_embed.index_count(wa_search._VEC_PATH)} vectors")

# Hybrid mode — fuses both; should return hits and a valid shape.
hyb = wa_search.query_messages(query="pizza", mode="hybrid", top_k=10)
check("hybrid returns hits", len(hyb) > 0, f"{len(hyb)} hits")
check("hybrid results best-first have rowids", all("_rowid" in r for r in hyb))

# from_me filter.
mine = wa_search.query_messages(query="pizza", mode="hybrid", from_me=True, top_k=10)
check("from_me=True yields only sent", all(r["from_me"] is True for r in mine),
      f"{len(mine)} hits")

# Context expansion adds neighbours and marks matches.
ctx = wa_search.query_messages(query="pizza", mode="lexical", top_k=3, context=2)
check("context expansion adds rows", len(ctx) >= len(lex[:3]) if lex else True)
check("context marks matches", any(r.get("is_match") for r in ctx))

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}", file=sys.stderr)
sys.exit(1 if fails else 0)
