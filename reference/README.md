# Reference material & attribution

Prior work that the technique in `wa_search.py` is based on. The third-party
scripts themselves are **not redistributed in this repository** (they remain the
copyright of their authors) — this file records attribution and links.

## ZAPiXDESK

PowerShell forensic extractors for WhatsApp Desktop (Windows UWP) by
**Alberto Magno (kraftdenker)** — <https://github.com/kraftdenker/ZAPiXDESK>.
Copyright 2025–2026 Alberto Magno. Referenced, not redistributed.

They implement the same core idea our tool uses — decrypt WhatsApp's SQLite
databases without SQLite SEE — derived from:

> Giyoon Kim, Uk Hur, Soojin Kang, Jongsung Kim. *Analyzing the Web and UWP
> versions of WhatsApp for digital forensics.* Forensic Science International:
> Digital Investigation, Vol. 52 (2025), 301861.
> <https://doi.org/10.1016/j.fsidi.2024.301861>

`wa_search.py` diverges by recovering the DB key via a live process-memory
scan (rather than the DPAPI-NG → session.db → nativeSettings.db key chain) and
by reading messages straight from the encrypted B-tree read-only.
