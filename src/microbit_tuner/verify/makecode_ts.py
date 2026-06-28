"""Verify MakeCode TypeScript with the fast tier (`makecode build -j`).

`makecode build -j` is JavaScript-only: it type-checks the candidate against the
micro:bit API and skips the native hex build. That is the cheapest tool that
answers "do these APIs exist and type-check?" — the AGENTS.md fast-path rule.

The candidate is written into the cached mkc project's `main.ts` and built with
cwd set to that project. Exit code is the source of truth for ok/!ok; stdout
lines are parsed for diagnostics.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional, Union

from . import _toolchain
from .result import CompileResult, Diagnostic

# main.ts(2,1): error TS2304: Cannot find name 'foo'.
_DIAG_RE = re.compile(
    r"^main\.ts\((\d+),(\d+)\):\s+(error|warning)\s+(TS\d+):\s+(.*)$"
)


def _read(code_or_path: Union[str, Path]) -> str:
    if isinstance(code_or_path, Path):
        return code_or_path.read_text(encoding="utf-8")
    # A str that is an existing file path is treated as a path for convenience;
    # otherwise it's literal source. Guard against multi-line "paths".
    if "\n" not in code_or_path:
        p = Path(code_or_path)
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8")
    return code_or_path


def verify_makecode_ts(code_or_path: Union[str, Path],
                       dependencies: Optional[dict] = None) -> CompileResult:
    """Type-check one MakeCode TypeScript program. Accepts source or a path.

    ``dependencies`` is the program's declared ``pxt.json`` dependency map
    (``name -> spec``), as captured in the golden corpus. The candidate is built
    against exactly those extensions; one it uses but does not declare is a real
    build failure. ``None`` uses the default project (synthetic/raw code).
    """
    _toolchain.require_ready()
    source = _read(code_or_path)
    # Variants are cached per dependency set, so this is a one-time build cost.
    project = _toolchain.mkc_project(dependencies)
    makecode = _toolchain.makecode_bin()

    (project / "main.ts").write_text(source, encoding="utf-8")

    proc = subprocess.run(
        [str(makecode), "build", "-j", "--no-colors"],
        cwd=project, capture_output=True, text=True,
    )
    raw = proc.stdout + proc.stderr

    diagnostics: list[Diagnostic] = []
    for line in raw.splitlines():
        m = _DIAG_RE.match(line.strip())
        if m:
            ln, col, sev, code, msg = m.groups()
            diagnostics.append(Diagnostic(
                line=int(ln), col=int(col), code=code, message=msg, severity=sev,
            ))

    ok = proc.returncode == 0
    return CompileResult(ok=ok, diagnostics=diagnostics, tool="makecode", raw=raw)
