"""Locate, check, and (only via `setup`) install the Node verification toolchain.

The Node tools (the `makecode` + `pyright` CLIs) and the micro:bit stubs live
under `src/microbit_tuner/verify/`. This module does **not** self-heal on demand:

  - `setup()` is the single place that installs anything.
  - `require_ready()` is a one-shot check that everything is present; verification
    calls it first (phase 1) and fails fast — pointing at `mbtuner setup` — rather
    than limping along with lazy fallbacks.

The only thing built lazily is a per-dependency-set MakeCode project (that's
verification *work*, not toolchain install: it assumes the toolchain is ready).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# .../src/microbit_tuner/verify/_toolchain.py  ->  verify dir
VERIFY_DIR = Path(__file__).resolve().parent
REPO_ROOT = VERIFY_DIR.parents[2]

NODE_BIN = VERIFY_DIR / "node_modules" / ".bin"
MAKECODE = NODE_BIN / "makecode"
PYRIGHT = NODE_BIN / "pyright"
STUBS_DIR = VERIFY_DIR / "vendor" / "microbit-stubs"
# A concrete file that only exists once the submodule is checked out.
STUBS_SENTINEL = STUBS_DIR / "lang" / "en" / "typeshed" / "stdlib" / "microbit" / "__init__.pyi"
PYRIGHT_CONFIG = VERIFY_DIR / "pyrightconfig.json"

# MakeCode projects (downloaded target + libs) live in the git-ignored cache.
# The base project is the default `init`; each distinct dependency set gets a
# hash-suffixed sibling, built once and cached on disk.
MKC_CACHE = REPO_ROOT / "cache" / "makecode-projects"
MKC_BASE_PROJECT = MKC_CACHE / "microbit-project"
MKC_BASE_SENTINEL = MKC_BASE_PROJECT / "pxt.json"


class ToolchainError(RuntimeError):
    """The verification toolchain isn't usable (not set up, or an install failed)."""


def _run(cmd: list, *, cwd: Path, what: str) -> None:
    print(f"  · {what} …", file=sys.stderr)
    proc = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ToolchainError(
            f"{what} failed (exit {proc.returncode}):\n"
            f"  cmd: {' '.join(str(c) for c in cmd)}\n{proc.stdout}\n{proc.stderr}"
        )


def _require(executable: str, hint: str) -> str:
    path = shutil.which(executable)
    if path is None:
        raise ToolchainError(f"'{executable}' not found on PATH. {hint}")
    return path


# --------------------------------------------------------------------------- #
# Readiness check (phase 1) — one look at everything, no side effects
# --------------------------------------------------------------------------- #

def missing() -> list[str]:
    """Return the missing toolchain pieces (empty list == ready)."""
    problems = []
    if shutil.which("node") is None:
        problems.append("Node.js (`node` not on PATH)")
    if not MAKECODE.exists():
        problems.append("makecode CLI (npm dependencies not installed)")
    if not PYRIGHT.exists():
        problems.append("pyright CLI (npm dependencies not installed)")
    if not STUBS_SENTINEL.exists():
        problems.append("micro:bit stubs (git submodule not checked out)")
    if not MKC_BASE_SENTINEL.exists():
        problems.append("MakeCode target (project not initialised)")
    return problems


def require_ready() -> None:
    """Raise a helpful ToolchainError if the toolchain isn't fully set up."""
    problems = missing()
    if problems:
        raise ToolchainError(
            "verification toolchain is not set up:\n  - "
            + "\n  - ".join(problems)
            + "\nrun `uv run mbtuner setup` first."
        )


# --------------------------------------------------------------------------- #
# The single install path
# --------------------------------------------------------------------------- #

def setup() -> None:
    """Install the verification toolchain. Idempotent; the only installer.

    Does not touch the Python environment — that is the caller's to manage.
    """
    git = _require("git", "Install git to fetch the micro:bit stubs submodule.")
    npm = _require("npm", "Install Node.js (which provides npm).")

    # 1. stubs submodule (source), 2. npm tools, 3. makecode target (needs npm).
    if not STUBS_SENTINEL.exists():
        rel = STUBS_DIR.relative_to(REPO_ROOT)
        _run([git, "submodule", "update", "--init", "--recursive", "--", str(rel)],
             cwd=REPO_ROOT, what="git submodule update (micro:bit stubs)")
    if not NODE_BIN.exists():
        _run([npm, "install"], cwd=VERIFY_DIR, what="npm install (makecode + pyright)")
    if not MKC_BASE_SENTINEL.exists():
        MKC_BASE_PROJECT.mkdir(parents=True, exist_ok=True)
        _run([MAKECODE, "init", "microbit"], cwd=MKC_BASE_PROJECT,
             what="makecode init (download MakeCode target)")

    still_missing = missing()
    if still_missing:
        raise ToolchainError(
            "setup ran but the toolchain is still incomplete:\n  - "
            + "\n  - ".join(still_missing)
        )


# --------------------------------------------------------------------------- #
# Accessors + per-input project build (assume the toolchain is ready)
# --------------------------------------------------------------------------- #

def pyright_setup() -> tuple[Path, Path]:
    """Return (pyright binary, pyright config)."""
    return PYRIGHT, PYRIGHT_CONFIG


def mkc_project(dependencies: Optional[dict] = None) -> Path:
    """Return the MakeCode project dir to build a candidate against.

    ``dependencies`` is the program's declared ``pxt.json`` map (``name -> spec``).
    ``None`` uses the base project (raw/synthetic code with no declared deps);
    otherwise a per-dep-set variant is built once and cached on disk. Resolving a
    variant's extensions may need network (e.g. github extensions). This is
    verification work and assumes :func:`require_ready` has already passed.
    """
    if not dependencies:
        return MKC_BASE_PROJECT

    deps = tuple(sorted(dependencies.items()))
    key = hashlib.sha1(repr(deps).encode()).hexdigest()[:12]
    project = MKC_CACHE / f"microbit-project__{key}"
    sentinel = project / "pxt.json"
    if sentinel.exists():
        return project

    project.mkdir(parents=True, exist_ok=True)
    _run([MAKECODE, "init", "microbit"], cwd=project,
         what=f"makecode init ({project.name})")
    # Set dependencies to exactly the declared set, then resolve them.
    config = json.loads(sentinel.read_text(encoding="utf-8"))
    config["dependencies"] = dict(deps)
    sentinel.write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")
    _run([MAKECODE, "install"], cwd=project, what=f"makecode install ({project.name})")
    return project


def makecode_bin() -> Path:
    return MAKECODE
