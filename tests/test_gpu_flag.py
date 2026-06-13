# /// script
# requires-python = ">=3.10"
# dependencies = ["fastembed", "sqlite-vec", "numpy", "onnxruntime"]
# ///
"""Test GPU provider plumbing in wa_embed: safe default + provider kwarg flow.

The default onnxruntime here is CPU-only, so the GPU helpers must report 'no GPU'
and leave embedding on CPU (the NVIDIA/AMD/CPU fresh-setup safety path). We then
force an explicit provider to prove the providers kwarg reaches model load — the
same code path DirectML/CUDA take when their onnxruntime is installed.
"""
import os
import sys
import tempfile

# Make the repo-root modules importable when run from tests/.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import wa_embed

fails = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}",
          file=sys.stderr)
    if not cond:
        fails.append(name)

provs = wa_embed.available_providers()
check("onnxruntime providers visible", "CPUExecutionProvider" in provs, str(provs))

# CPU-only build: --directml must report unavailable and leave CPU (no hard fail).
check("enable_directml False on CPU-only", wa_embed.enable_directml(verbose=True) is False)
check("providers default after directml", wa_embed._PROVIDERS is None)

# CPU-only build: --gpu (auto) must return None and leave CPU
# (this is the NVIDIA-or-CPU fresh-setup safety path).
check("enable_gpu None on CPU-only", wa_embed.enable_gpu(verbose=True) is None)
check("providers default after gpu", wa_embed._PROVIDERS is None)

# Prove the providers kwarg flows into model load by forcing an explicit provider
# (CPU here; DmlExecutionProvider/CUDA take the exact same path on a GPU box).
wa_embed.use_providers(["CPUExecutionProvider"])
check("use_providers set", wa_embed._PROVIDERS == ["CPUExecutionProvider"])
tmp = os.path.join(tempfile.mkdtemp(), "v.db")
n = wa_embed.build_index([(1, "hello there"), (2, "goodbye now")], tmp)
check("model builds with explicit provider", n == 2 and wa_embed.index_count(tmp) == 2, f"n={n}")
check("search works with explicit provider", len(wa_embed.search("hello", tmp, top_k=2)) > 0)

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}", file=sys.stderr)
sys.exit(1 if fails else 0)
