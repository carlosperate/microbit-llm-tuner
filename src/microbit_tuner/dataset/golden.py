"""Stage 2 — golden assembly.

Turns the collected scrape (``data/collected/``) into the curated golden corpus
(``data/golden/``): one file per project carrying a human-authored ``task`` plus
verified/tweaked code. Golden is the hand-owned source of truth — a human writes
the task and checks every entry; this module only scaffolds and gates it.

Two operations, kept strictly apart:

- :func:`sync_golden` — **add-only**. Creates a stub for every collected
  project that has no golden file yet, carrying just the curated core
  (``slug``/``task``/``code``) plus ``url`` + ``license`` provenance; editorial
  ``title``/``meta``/``context`` stay in ``collected``. It never touches an
  existing golden file, so a human's task and code edits are safe across re-runs
  (e.g. after new projects are scraped).

- :func:`gate_golden` — the **gate**. For every golden entry it checks the task
  is non-empty and that every program still compiles (fast tier, the same
  verifier as stage 3), then reports the verdict per entry. It is read-only —
  the golden files are never written; a human owns every field. Compilation is
  the real gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from ..collect import COLLECTED_DIR, GOLDEN_DIR, _LANG_KEYS, _record_samples
from ..verify import verify_makecode_ts, verify_micropython


def _write_json(path: Path, record: dict) -> None:
    """Write a record in the repo's canonical JSON style (UTF-8, 2-space, LF)."""
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _programs_from_code(code: dict) -> list[dict]:
    """Group a collected ``code`` map into golden ``programs`` by program_index.

    Collected stores ``{lang_key: [block, ...]}`` (one block per program_index);
    golden stores a list of programs, each carrying its own ``task`` and at most
    one solution per language. Blocks sharing a program_index describe the same
    program in different languages, so they collapse into one stub program with
    an empty ``task`` for a human to author.
    """
    by_index: dict = {}
    for lang_key in _LANG_KEYS:
        for i, block in enumerate(code.get(lang_key) or []):
            idx = block.get("program_index", i)
            clean = {k: block[k] for k in ("pub_id", "source", "dependencies") if k in block}
            by_index.setdefault(idx, {})[lang_key] = clean
    programs = []
    for idx in sorted(by_index):
        program = {"task": ""}
        for lang_key in _LANG_KEYS:
            if lang_key in by_index[idx]:
                program[lang_key] = by_index[idx][lang_key]
        programs.append(program)
    return programs


def _stub_from_collected(collected: dict, slug: str) -> dict:
    """Build a golden stub from a collected record.

    Golden carries only the curated/derived core plus provenance: ``slug`` (the
    id), ``url`` + ``license`` for attribution, and a ``programs`` list. Each
    program holds a human-owned ``task`` and at most one solution per language (a
    TS solution also keeps its own ``pub_id`` / ``dependencies``); programs are
    grouped from collected by program_index. Editorial fields (``title``,
    ``meta``, ``context``) and the scrape ``errors`` audit are intentionally
    *not* copied: they stay in ``collected/<slug>.json`` under the same slug and
    are joined there if a later stage needs them. A program's ``source`` may
    diverge from collected once a human tweaks it — that is why it lives here.
    """
    return {
        "slug": slug,
        "url": collected.get("url", ""),
        "license": collected.get("license", ""),
        "programs": _programs_from_code(collected.get("code", {})),
    }


def sync_golden(
    collected_dir: Path = COLLECTED_DIR, golden_dir: Path = GOLDEN_DIR
) -> tuple[list[str], list[str]]:
    """Create a golden stub for every collected project lacking one.

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
    """Gate every golden entry and report whether it passes.

    Yields one result dict per entry as it is checked (so a caller can stream
    progress)::

        {"slug", "passed": bool, "task_ok": bool, "code_ok": bool,
         "diagnostics": [str, ...]}

    An entry passes iff *every* program carries a non-empty ``task`` **and**
    every program compiles. The gate is read-only — it never writes to the
    golden files.
    """
    for path in sorted(golden_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        record = json.loads(path.read_text(encoding="utf-8"))

        programs = record.get("programs") or []
        task_ok = bool(programs) and all(
            str(p.get("task", "")).strip() for p in programs
        )
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

        yield {
            "slug": path.stem,
            "passed": task_ok and code_ok,
            "task_ok": task_ok,
            "code_ok": code_ok,
            "diagnostics": diagnostics,
        }
