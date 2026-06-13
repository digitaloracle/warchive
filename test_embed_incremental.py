# /// script
# requires-python = ">=3.10"
# dependencies = ["fastembed", "sqlite-vec", "numpy"]
# ///
"""Fast unit test for incremental embedding (wa_embed.indexed_ids / add_index)."""
import os
import sys
import tempfile

import wa_embed

fails = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}",
          file=sys.stderr)
    if not cond:
        fails.append(name)

if not wa_embed.EMBED_AVAILABLE:
    print("SKIP: embeddings unavailable", file=sys.stderr); sys.exit(0)

tmp = os.path.join(tempfile.mkdtemp(), "vec.db")

# Initial small build (rowids 1..5).
n = wa_embed.build_index([(i, f"message number {i} about cats") for i in range(1, 6)], tmp)
check("initial build count", n == 5, f"n={n}")
check("indexed_ids == 1..5", wa_embed.indexed_ids(tmp) == {1, 2, 3, 4, 5})

# Incremental add: 3 old (skipped) + 3 new (6,7,8).
added = wa_embed.add_index([(i, f"message number {i} about dogs") for i in range(3, 9)], tmp)
check("add_index returns only NEW count", added == 3, f"added={added}")
check("index grew to 8", wa_embed.index_count(tmp) == 8, f"count={wa_embed.index_count(tmp)}")
check("indexed_ids == 1..8", wa_embed.indexed_ids(tmp) == set(range(1, 9)))

# Re-add same -> nothing new.
again = wa_embed.add_index([(i, "x") for i in range(1, 9)], tmp)
check("re-add adds nothing", again == 0, f"added={again}")

# add_index on a non-existent index behaves like build.
tmp2 = os.path.join(tempfile.mkdtemp(), "vec2.db")
b = wa_embed.add_index([(10, "hello world"), (11, "another one")], tmp2)
check("add_index bootstraps when no index", b == 2 and wa_embed.index_count(tmp2) == 2, f"b={b}")

# Search still works after incremental.
hits = wa_embed.search("dogs", tmp, top_k=3)
check("search works post-incremental", len(hits) > 0, f"{len(hits)} hits")

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}", file=sys.stderr)
sys.exit(1 if fails else 0)
