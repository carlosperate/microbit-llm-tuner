"""mbtuner — task runner for the micro:bit LLM tuner pipeline.

Each subcommand maps to a model tuner pipeline stage.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, NoReturn, Optional

import typer

from .verify import (
    ToolchainError, verify_makecode_ts, verify_micropython,
    setup as setup_toolchain,
)

# .../src/microbit_tuner/cli.py  ->  repo root (editable install).
REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_DIR = REPO_ROOT / "src" / "microbit_tuner" / "verify"

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)


def _fail(message: str) -> NoReturn:
    """Print a helpful error to stderr and exit 2 (usage/environment error)."""
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(2)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def collect(ctx: typer.Context) -> None:
    """Stage 1: scrape micro:bit projects into the collected corpus.

    Writes one merged JSON per project to data/collected/. Extra arguments are
    forwarded to the scraper, e.g. `--force`, `--only SLUG`, `--out DIR`,
    `--delay SEC`.
    """
    from .collect.scraper import main as scraper_main
    raise typer.Exit(scraper_main(ctx.args))


class Lang(str, Enum):
    ts = "ts"
    py = "py"


class Target(str, Enum):
    collected = "collected"
    golden = "golden"
    synthetic = "synthetic"


def _read_file(file: Path) -> str:
    """Read a source file, turning every failure into a clean exit-2 message."""
    if file.is_dir():
        _fail(f"{file} is a directory; pass a single source file.")
    if not file.exists():
        _fail(f"no such file: {file}")
    try:
        return file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"could not read {file}: {exc}")


def _parse_deps(items: Optional[list[str]]) -> Optional[dict]:
    """Parse repeated `--dep NAME[=SPEC]` into a dependency map."""
    if not items:
        return None
    deps: dict[str, str] = {}
    for item in items:
        name, sep, spec = item.partition("=")
        name = name.strip()
        if not name:
            _fail(f"invalid --dep {item!r}; expected NAME or NAME=SPEC")
        deps[name] = spec.strip() if sep and spec.strip() else "*"
    return deps


def _run_one(source: str, language: str, deps: Optional[dict]):
    """Dispatch a single program to the right verifier."""
    if language == "ts":
        return verify_makecode_ts(source, deps)
    return verify_micropython(source)


def _emit(result, name: str, as_json: bool) -> None:
    """Render a single CompileResult to stdout."""
    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=2))
        return
    typer.echo(f"{name}: {result.summary()}")
    for d in result.diagnostics:
        typer.echo(f"  {d}")
    # A failure we couldn't parse into diagnostics: don't leave the user with an
    # opaque "FAIL: 0 errors" — show the tail of the raw tool output.
    if not result.ok and not result.diagnostics:
        tail = "\n".join(result.raw.strip().splitlines()[-8:])
        if tail:
            typer.echo("  (no diagnostics parsed; raw tool output below)", err=True)
            typer.echo("  " + tail.replace("\n", "\n  "), err=True)


def _verify_samples(samples, as_json: bool) -> int:
    """Verify a stream of CodeSamples (lang, source, dependencies, label).

    Returns an exit code: 0 if everything passed, 1 if anything failed. In text
    mode it prints one status line per program as it is checked (each echo
    flushes), so a long scan shows live progress instead of looking stuck; a
    failing program also lists its first diagnostics. JSON mode stays silent
    until the final report so stdout holds only the JSON.
    """
    passed = total = 0
    failures: list[dict] = []
    for s in samples:
        total += 1
        result = _run_one(s.source, s.lang, s.dependencies)
        if result.ok:
            passed += 1
        else:
            failures.append({"label": s.label, "tool": result.tool,
                             "diagnostics": [str(d) for d in result.errors]})
        if not as_json:
            status = "ok  " if result.ok else "FAIL"
            suffix = "" if result.ok else f" ({result.tool})"
            typer.echo(f"[{total:>4}] {status} {s.label}{suffix}")
            if not result.ok:
                for d in result.errors[:5]:
                    typer.echo(f"           {d}")
    if as_json:
        typer.echo(json.dumps({"passed": passed, "total": total,
                               "failures": failures}, indent=2))
    else:
        typer.echo(f"{passed}/{total} passed")
    return 0 if passed == total else 1


@app.command(no_args_is_help=True)
def verify(
    file: Annotated[Optional[Path], typer.Argument(
        help="Source file to verify; language comes from its .ts/.py extension.")] = None,
    stdin: Annotated[Optional[Lang], typer.Option(
        "--stdin", metavar="LANG",
        help="Read code from stdin instead of a file; LANG (ts|py) is the "
             "language.")] = None,
    target: Annotated[Optional[Target], typer.Option(
        "--target", metavar="DATASET",
        help="Verify every program in a corpus: collected | golden | synthetic.")] = None,
    dep: Annotated[Optional[list[str]], typer.Option(
        "--dep", metavar="NAME[=SPEC]",
        help="MakeCode extension to build against, repeatable (TS only). Only "
             "for a single FILE or --stdin; corpus deps come from the data.")] = None,
    as_json: Annotated[bool, typer.Option(
        "--json", help="Emit the result(s) as JSON.")] = False,
) -> None:
    """Stage 3: check that micro:bit code compiles.

    Pick exactly one input: a FILE, --stdin LANG, or --target DATASET. MakeCode
    TypeScript is checked with the makecode CLI, MicroPython with Pyright. Exits
    0 if everything compiles, 1 on diagnostics, 2 on a usage or toolchain error.
    """
    modes = [file is not None, stdin is not None, target is not None]
    if sum(modes) > 1:
        _fail("choose only one of: FILE, --stdin LANG, --target DATASET")
    if sum(modes) == 0:
        _fail("nothing to verify: pass a FILE, --stdin LANG, or --target DATASET")

    try:
        if target is not None:
            if dep:
                _fail("--dep is only valid with a single FILE or --stdin")
            from .collect import iter_samples
            raise typer.Exit(_verify_samples(iter_samples(target.value), as_json))

        if stdin is not None:
            source = sys.stdin.read()
            if not source.strip():
                _fail("no code received on stdin.")
            language, name = stdin.value, "<stdin>"
        else:
            assert file is not None
            source = _read_file(file)
            language = {".ts": "ts", ".py": "py"}.get(file.suffix.lower())
            if language is None:
                _fail(f"cannot determine language from extension of {file.name!r}; "
                      "rename to .ts/.py or use --stdin ts|py")
            name = str(file)

        result = _run_one(source, language, _parse_deps(dep))
    except ToolchainError as exc:
        _fail(str(exc))
    _emit(result, name, as_json)
    raise typer.Exit(0 if result.ok else 1)


@app.command()
def viewer(port: int = 8000) -> None:
    """Serve the repo over HTTP so tools/viewer.html can browse the corpus."""
    typer.echo(f"Serving {REPO_ROOT} at http://localhost:{port}/tools/viewer.html")
    rc = subprocess.run(
        [sys.executable, "-m", "http.server", str(port)], cwd=REPO_ROOT
    ).returncode
    raise typer.Exit(rc)


@app.command()
def setup() -> None:
    """Install the stage-3 verification toolchain. Run this once after cloning.

    Performs three idempotent steps:

      1. `git submodule update --init` — checks out the official micro:bit type
         stubs that MicroPython verification type-checks against.
      2. `npm install` — installs the makecode and pyright command-line tools.
      3. `makecode init` — downloads the MakeCode micro:bit target into
         cache/makecode-projects/ (needs the tools from step 2; the first compile
         would otherwise be slow).

    It does NOT touch your Python environment — you manage that yourself (e.g.
    `uv sync`). Requires `node`/`npm` and `git` on PATH; it errors out if either
    is missing rather than half-installing. Rerun it after `mbtuner clean`.
    """
    try:
        setup_toolchain()
    except ToolchainError as exc:
        _fail(str(exc))
    typer.echo("Verification toolchain ready.")


@app.command()
def clean(
    all_: Annotated[bool, typer.Option(
        "--all", help="Also drop data/collected (regenerable scrape) and models/, "
                      "not just caches.")] = False,
) -> None:
    """Remove regenerable caches and build artifacts.

    Deletes the cache/ folder (scraper responses + MakeCode projects), the verify
    toolchain (node_modules), and __pycache__ dirs. Your Python venv is never
    touched. The hand-committed golden corpus is always kept; the regenerable
    collected scrape and trained models are kept unless `--all` is given.
    """
    targets = [
        VERIFY_DIR / "node_modules",
        REPO_ROOT / "cache",
    ]
    if all_:
        targets += [REPO_ROOT / "data" / "collected", REPO_ROOT / "models"]

    for t in targets:
        if t.exists():
            typer.echo(f"  rm {t.relative_to(REPO_ROOT)}")
            shutil.rmtree(t)

    pyc = 0
    for p in REPO_ROOT.rglob("__pycache__"):
        if (VERIFY_DIR / "node_modules") in p.parents:
            continue
        shutil.rmtree(p, ignore_errors=True)
        pyc += 1
    if pyc:
        typer.echo(f"  rm {pyc} __pycache__ dir(s)")
    typer.echo("Clean complete." + ("" if all_ else
               "  (use --all to also drop data/collected, models)"))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
