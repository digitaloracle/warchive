# tests

Run from the repo root.

## Portable (stdlib only — run anywhere)

```bash
python tests/test_wa_normalize.py     # normalization + Hebrew stemming
python tests/test_wa_translit.py      # Hebrew↔Latin transliteration / expansion
```

## Need the embedding extras (run via uv; synthetic data, no WhatsApp required)

```bash
uv run --script tests/test_gpu_flag.py          # GPU provider plumbing + CPU fallback
uv run --script tests/test_embed_incremental.py # incremental vector index add
```

## Need a built mirror (your own WhatsApp data) + uv

```bash
uv run --script tests/test_wa_mcp.py   # MCP tool smoke test (queries the local mirror)
```

These exercise behaviour against the live `wa_mirror.db`, so they only pass on a
machine that has run warchive against a real WhatsApp Desktop install. They are
verification scripts, not CI-portable tests.
