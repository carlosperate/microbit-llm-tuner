"""Stage 1 — data collection.

The scraper (``scraper.py``) writes the collected corpus; this package also
exposes the *read* side so later stages don't need to know its on-disk layout.
:func:`iter_samples` yields every code block in a dataset as a
:class:`CodeSample`, ready to hand straight to the stage-3 verifier.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, NamedTuple

# .../src/microbit_tuner/collect/__init__.py  ->  repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
COLLECTED_DIR = DATA_DIR / "collected"   # regenerable scrape (this stage's output)
GOLDEN_DIR = DATA_DIR / "golden"         # curated task + code (stage 2)

# Map a record's `code` language key to the short lang tag the verifier expects.
_LANG_KEYS = {"makecode_ts": "ts", "micropython": "py"}


class CodeSample(NamedTuple):
    """One verifiable program extracted from a dataset."""

    lang: str           # "ts" | "py"
    source: str
    dependencies: dict  # MakeCode pxt.json deps for TS; {} for MicroPython
    label: str          # human-readable id, e.g. "beating-heart[makecode_ts:0]"


def _record_samples(record: dict, slug: str) -> Iterator[CodeSample]:
    """Yield the CodeSamples in one record, tolerating both on-disk shapes.

    - collected / golden: ``code`` is a map ``{lang_key: [block, ...]}`` (a
      golden block may be a bare source string rather than a ``{source: ...}``
      dict, for hand-flattened single-solution entries).
    - synthetic: ``code`` is a single source string with a top-level
      ``language`` key.
    """
    code = record.get("code")
    if isinstance(code, dict):
        for lang_key, blocks in code.items():
            lang = _LANG_KEYS.get(lang_key)
            if lang is None:
                continue
            for i, block in enumerate(blocks if isinstance(blocks, list) else [blocks]):
                if isinstance(block, str):
                    block = {"source": block}
                yield CodeSample(
                    lang=lang,
                    source=block["source"],
                    dependencies=block.get("dependencies", {}),
                    label=f"{slug}[{lang_key}:{block.get('program_index', i)}]",
                )
    elif isinstance(code, str):
        lang = _LANG_KEYS.get(record.get("language", ""))
        if lang is not None:
            yield CodeSample(
                lang=lang,
                source=code,
                dependencies=record.get("dependencies", {}),
                label=slug,
            )


def iter_samples(dataset: str = "collected") -> Iterator[CodeSample]:
    """Yield every code block in ``data/<dataset>/`` as a CodeSample.

    Knows the on-disk JSON schema (it is produced here) so callers — e.g. the
    verifier — don't have to. Works across ``collected``, ``golden`` and
    ``synthetic``; ``manifest.json`` is skipped. A missing dataset folder yields
    nothing.
    """
    folder = DATA_DIR / dataset
    for path in sorted(folder.glob("*.json")):
        if path.name == "manifest.json":
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        yield from _record_samples(record, path.stem)
