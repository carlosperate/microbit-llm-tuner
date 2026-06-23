# microbit-llm-tuner

Building a small, specialised language model that generates micro:bit code and
runs entirely in the browser. See [AGENTS.md](AGENTS.md) for the full project
rationale.

## Stage 1: project scraper (`scraper.py`)

A scraper for microbit.org's
[Make it: code it](https://microbit.org/projects/make-it-code-it/) collection.
It produces JSON files in the `golden` folder for the verified seed/floor
for fine-tuning.

Two sets of data samples, one for MicroPython, another for MakeCode TypeScript.

### Usage

```bash
uv sync
uv run python scraper.py
```

Useful flags:

| Flag | Purpose |
| --- | --- |
| `--out DIR` | output directory (default `golden`); manifest is `DIR/manifest.json` |
| `--cache DIR` | cache directory for raw HTML / API JSON (default `cache`) |
| `--delay SEC` | delay before each *network* request (default `0.7`; cache hits skip it) |
| `--force` | re-process and overwrite outputs even if present |
| `--only SLUG …` | only these slugs (e.g. re-scrape one project with `--force`) |

The run is cached (re-runs don't re-hit the site) and resumable.

### Output layout

```
golden/
  typescript/<slug>.json     # language: "makecode_ts" — main.ts per pub_id
  micropython/<slug>.json    # language: "micropython" — inline page block(s)
  manifest.json              # top-level index + summary counts
```
