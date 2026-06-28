"""Stage 3 — deterministic verification by compilation.

Takes a file or a string of micro:bit code, compiles/type-checks it with the
fast tier, and returns success or a structured list of errors. The two
paradigms never merge: MakeCode TypeScript and MicroPython have separate
verifiers but share one result shape.

No language model is involved anywhere in this path (AGENTS.md). The compiler /
type-checker is the ground truth; we only report its facts.

Public API:
    verify_makecode_ts(code_or_path, dependencies=None) -> CompileResult
    verify_micropython(code_or_path) -> CompileResult
    CompileResult, Diagnostic
    ToolchainError  # raised when the env can't run the tools (vs. a compile fail)
"""

from __future__ import annotations

from .result import CompileResult, Diagnostic
from .makecode_ts import verify_makecode_ts
from .micropython import verify_micropython
from ._toolchain import ToolchainError, setup, missing

__all__ = [
    "CompileResult",
    "Diagnostic",
    "ToolchainError",
    "verify_makecode_ts",
    "verify_micropython",
    "setup",
    "missing",
]
