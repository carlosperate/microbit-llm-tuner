"""Stage 2 — golden assembly.

Turns the collected scrape (``data/collected/``) into the curated golden corpus
(``data/golden/``): one file per project carrying a human-authored ``task`` plus
verified/tweaked code. Golden is the hand-owned source of truth — a human writes
the task and checks every entry; this module only scaffolds and gates it.

Two operations, kept strictly apart:

- :func:`sync_golden` — **add-only**. Creates a draft stub for every collected
  project that has no golden file yet, carrying just the curated core
  (``slug``/``task``/``status``/``code``) plus ``url`` + ``license`` provenance;
  editorial ``title``/``meta``/``context`` stay in ``collected``. It never
  touches an existing golden file, so a human's task and code edits are safe
  across re-runs (e.g. after new projects are scraped).

- :func:`gate_golden` — the **gate**. For every golden entry it checks the task
  is non-empty and that every program still compiles (fast tier, the same
  verifier as stage 3), then writes the resolved ``status`` back
  (``verified`` / ``draft``). Only the derived ``status`` field is ever changed;
  human-authored ``task`` and ``code`` are never modified. Compilation is the
  real gate — no language model is involved in the verdict.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from ..collect import COLLECTED_DIR, GOLDEN_DIR, _record_samples
from ..verify import verify_makecode_ts, verify_micropython


def _write_json(path: Path, record: dict) -> None:
    """Write a record in the repo's canonical JSON style (UTF-8, 2-space, LF)."""
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _stub_from_collected(collected: dict, slug: str) -> dict:
    """Build a draft golden stub from a collected record.

    Golden carries only the curated/derived core plus provenance: ``slug`` (the
    id), the human-owned ``task`` and ``code``, the gate's ``status``, and
    ``url`` + ``license`` for attribution (each TS program also keeps its own
    ``pub_id`` / ``dependencies``). Editorial fields (``title``, ``meta``,
    ``context``) and the scrape ``errors`` audit are intentionally *not* copied:
    they stay in ``collected/<slug>.json`` under the same slug and are joined
    there if a later stage needs them. ``code`` may diverge from collected once a
    human tweaks it — that is why it lives here.
    """
    return {
        "slug": slug,
        "task": "",
        "status": "draft",
        "url": collected.get("url", ""),
        "license": collected.get("license", ""),
        "code": collected.get("code", {}),
    }


def sync_golden(
    collected_dir: Path = COLLECTED_DIR, golden_dir: Path = GOLDEN_DIR
) -> tuple[list[str], list[str]]:
    """Create a draft golden stub for every collected project lacking one.

    Strictly add-only: returns ``(created_slugs, existing_slugs)`` and never
    writes over an existing golden file.
    """
    golden_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    existing: list[str] = []
    for path in sorted(collected_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        slug = path.stem
        dest = golden_dir / f"{slug}.json"
        if dest.exists():
            existing.append(slug)
            continue
        collected = json.loads(path.read_text(encoding="utf-8"))
        _write_json(dest, _stub_from_collected(collected, slug))
        created.append(slug)
    return created, existing


def gate_golden(golden_dir: Path = GOLDEN_DIR) -> Iterator[dict]:
    """Gate every golden entry and persist its ``status``.

    Yields one result dict per entry as it is checked (so a caller can stream
    progress)::

        {"slug", "verified": bool, "task_ok": bool, "code_ok": bool,
         "diagnostics": [str, ...]}

    An entry is ``verified`` iff its ``task`` is non-empty **and** every program
    compiles. The resolved status is written back to the file (only when it
    changed); nothing else in the file is touched.
    """
    for path in sorted(golden_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        record = json.loads(path.read_text(encoding="utf-8"))

        task_ok = bool(str(record.get("task", "")).strip())
        code_ok = True
        diagnostics: list[str] = []
        for sample in _record_samples(record, path.stem):
            result = (
                verify_makecode_ts(sample.source, sample.dependencies)
                if sample.lang == "ts"
                else verify_micropython(sample.source)
            )
            if not result.ok:
                code_ok = False
                for diag in result.errors:
                    diagnostics.append(f"{sample.label} ({result.tool}): {diag}")

        verified = task_ok and code_ok
        status = "verified" if verified else "draft"
        if record.get("status") != status:
            record["status"] = status
            _write_json(path, record)

        yield {
            "slug": path.stem,
            "verified": verified,
            "task_ok": task_ok,
            "code_ok": code_ok,
            "diagnostics": diagnostics,
        }
