# microbit-llm-tuner

Building a small, specialised language model that generates micro:bit code and
runs entirely in the browser. See [AGENTS.md](AGENTS.md) for the full project
rationale.

## Task runner (`mbtuner`)

`uv sync` installs the project (editable) and a console script, `mbtuner`, that
wraps each pipeline stage:

| Command | Does |
| --- | --- |
| `setup` | install the stage-3 verify toolchain (npm install + stubs submodule + makecode target); does not touch your Python env |
| `collect [args]` | stage 1 — the scraper (args forwarded) |
| `verify [args]` | stage 3 — verification (args forwarded) |
| `viewer [port]` | serve the repo so `tools/viewer.html` can browse the corpus |
| `clean [--all]` | remove regenerable caches (`--all` also drops `data/golden`, `models`) |

Run it through uv so the environment is active: `uv run mbtuner <command>`.

## Stage 1: project scraper (`scraper.py`)

A scraper for microbit.org's
[Make it: code it](https://microbit.org/projects/make-it-code-it/) collection.
It produces JSON files in the `data/golden` folder for the verified seed/floor
for fine-tuning.

Two sets of data samples, one for MicroPython, another for MakeCode TypeScript.

### Usage

```bash
uv sync
uv run mbtuner collect
```

Useful flags:

| Flag | Purpose |
| --- | --- |
| `--out DIR` | output directory (default `data/golden`); manifest is `DIR/manifest.json` |
| `--cache DIR` | cache directory for raw HTML / API JSON (default `cache/scrape`) |
| `--delay SEC` | delay before each *network* request (default `0.7`; cache hits skip it) |
| `--force` | re-process and overwrite outputs even if present |
| `--only SLUG …` | only these slugs (e.g. re-scrape one project with `--force`) |

The run is cached (re-runs don't re-hit the site) and resumable.

### Output layout

```
data/golden/
  typescript/<slug>.json     # language: "makecode_ts" — main.ts per pub_id
  micropython/<slug>.json    # language: "micropython" — inline page block(s)
  manifest.json              # top-level index + summary counts
```

Each TypeScript code block also records the program's `dependencies` (its
`pxt.json` extension map) — the authoritative list of MakeCode extensions it
needs, used directly by stage-3 verification.

The `tools/viewer.html` file is a standalone page to browse the corpus.

## Stage 3: verification by compilation (`verify/`)

Deterministically checks that a program compiles against the real micro:bit API,
with the fast tier — **MakeCode TypeScript** via the `makecode` CLI
(`build -j`), **MicroPython** via Pyright + the official
[micro:bit stubs](https://github.com/microbit-foundation/micropython-microbit-stubs)
(vendored as a git submodule). No language model is involved. Both return the
same `CompileResult` (`ok` + structured `Diagnostic`s).

### Setup

```bash
uv run mbtuner setup
```

This is required once before verifying. It runs `npm install` (the makecode +
pyright CLIs), `git submodule update --init` (the micro:bit stubs), and
`makecode init` (downloads the MakeCode target) — see `mbtuner setup --help`. It
does **not** touch your Python environment. Verification checks the toolchain is
present up front and fails fast telling you to run `setup` if anything's missing
(it never silently installs behind your back). Needs `node`/`npm` and `git`.

### Usage

Pick exactly one input:

```bash
uv run mbtuner verify path/to/main.ts          # a FILE (.ts -> MakeCode TS)
uv run mbtuner verify path/to/main.py          # a FILE (.py -> MicroPython)
printf '…' | uv run mbtuner verify --stdin py  # stdin; LANG is mandatory
uv run mbtuner verify --golden                 # the whole golden corpus
uv run mbtuner verify main.ts --dep datalogger # build against an extension
uv run mbtuner verify main.ts --json           # machine-readable result
```

Language comes from the `.ts`/`.py` extension for a file, or from `--stdin LANG`
for stdin — never guessed from content. With no input it prints help. Exit code:
`0` clean, `1` diagnostics, `2` usage/toolchain error.

For TypeScript, the program is built against exactly the extensions it declares
(`--dep`, or the `dependencies` recorded in the corpus). An extension that's used
but not declared is a genuine build failure — we never guess deps. `--golden`
pulls source via `collect.iter_sources`, so the verifier never has to know the
corpus layout (the dataset stage will expose the same iterator for `--dataset`).

The whole golden corpus compiles: **222/222** code blocks (120 TypeScript,
102 MicroPython).
