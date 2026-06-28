"""Verify MicroPython with Pyright + the official micro:bit stubs.

MicroPython is dynamic — nothing on-device checks the API at compile time. So
Pyright against the official `micropython-microbit-stubs` is the real analog to
MakeCode's compile-time API guarantee: it confirms `import microbit` and friends
resolve and are used with the right names/types.

`mpy-cross` (device-real bytecode compile) is a deliberate future add-on, not
built here.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Union

from . import _toolchain
from .result import CompileResult, Diagnostic


def _read(code_or_path: Union[str, Path]) -> str:
    if isinstance(code_or_path, Path):
        return code_or_path.read_text(encoding="utf-8")
    if "\n" not in code_or_path:
        p = Path(code_or_path)
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8")
    return code_or_path


def verify_micropython(code_or_path: Union[str, Path]) -> CompileResult:
    """Type-check one MicroPython program. Accepts source or a path."""
    _toolchain.require_ready()
    source = _read(code_or_path)
    pyright, config = _toolchain.pyright_setup()

    # Pyright analyses a file on disk; write the candidate to a temp .py.
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "candidate.py"
        target.write_text(source, encoding="utf-8")
        proc = subprocess.run(
            [str(pyright), "--outputjson", "-p", str(config), str(target)],
            capture_output=True, text=True,
        )
        raw = proc.stdout

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Pyright failed before producing JSON (config/install problem, etc.).
        return CompileResult(
            ok=False,
            diagnostics=[Diagnostic(1, 1, "pyright_error",
                                    "pyright produced no JSON output", "error")],
            tool="pyright", raw=proc.stdout + proc.stderr,
        )

    diagnostics: list[Diagnostic] = []
    for d in data.get("generalDiagnostics", []):
        start = d.get("range", {}).get("start", {})
        diagnostics.append(Diagnostic(
            line=int(start.get("line", 0)) + 1,    # 0-based -> 1-based
            col=int(start.get("character", 0)) + 1,
            code=d.get("rule", ""),
            message=d.get("message", ""),
            severity=d.get("severity", "error"),
        ))

    error_count = data.get("summary", {}).get("errorCount", len(
        [d for d in diagnostics if d.severity == "error"]))
    ok = error_count == 0
    return CompileResult(ok=ok, diagnostics=diagnostics, tool="pyright", raw=raw)
