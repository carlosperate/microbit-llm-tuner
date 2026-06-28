"""Stage 1 — data collection.

The scraper (``scraper.py``) writes the golden corpus; this package also exposes
the *read* side of that corpus so later stages don't need to know its on-disk
layout. :func:`iter_sources` yields every code block as a :class:`CodeSample`,
ready to hand straight to the stage-3 verifier.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, NamedTuple

# .../src/microbit_tuner/collect/__init__.py  ->  repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_DIR = REPO_ROOT / "data" / "golden"

# Which corpus sub-folder holds which language.
_LANG_FOLDERS = {"ts": "typescript", "py": "micropython"}


class CodeSample(NamedTuple):
    """One verifiable program extracted from the corpus."""

    lang: str           # "ts" | "py"
    source: str
    dependencies: dict  # MakeCode pxt.json deps for TS; {} for MicroPython
    label: str          # human-readable id, e.g. "beating-heart[0]"


def iter_sources(golden_dir: Path = GOLDEN_DIR) -> Iterator[CodeSample]:
    """Yield every code block in the golden corpus as a CodeSample.

    Knows the golden JSON schema (it is produced here) so callers — e.g. the
    verifier — don't have to. TS blocks carry their declared ``dependencies``;
    MicroPython blocks have none.
    """
    for lang, folder in _LANG_FOLDERS.items():
        for path in sorted((golden_dir / folder).glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            for block in record.get("code", []):
                yield CodeSample(
                    lang=lang,
                    source=block["source"],
                    dependencies=block.get("dependencies", {}),
                    label=f"{path.stem}[{block.get('block_index', 0)}]",
                )
