"""Shared result shape for both verifiers.

One dataclass for both languages so downstream stages (repair/discard, dataset
balancing) treat MakeCode TypeScript and MicroPython diagnostics identically.

Line/column are normalised to **1-based** for both tools. MakeCode's CLI already
reports 1-based `(line,col)`; Pyright reports 0-based JSON, which we add 1 to on
the way in. `code` is the tool's own rule id (e.g. `TS2304`,
`reportUndefinedVariable`) preserved verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Diagnostic:
    line: int           # 1-based
    col: int            # 1-based
    code: str           # tool rule id, verbatim (e.g. "TS2304", "reportUndefinedVariable")
    message: str
    severity: str       # "error" | "warning" | "information"

    def __str__(self) -> str:
        return f"{self.line}:{self.col} {self.severity} {self.code}: {self.message}"


@dataclass
class CompileResult:
    ok: bool
    diagnostics: list[Diagnostic] = field(default_factory=list)
    tool: str = ""      # "makecode" | "pyright"
    raw: str = ""       # raw tool output, for audit / debugging

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "diagnostics": [asdict(d) for d in self.diagnostics],
        }

    def summary(self) -> str:
        if self.ok:
            return f"OK ({self.tool})"
        n = len(self.errors)
        return f"FAIL ({self.tool}): {n} error(s)"
