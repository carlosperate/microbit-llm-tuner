#!/usr/bin/env python3
"""Deterministic scraper for microbit.org "Make it: code it" projects.

Produces a byte-exact code corpus as one merged JSON file per project, holding
both languages (MakeCode TypeScript and MicroPython) under a single `code` map.
No language model is involved anywhere in the extraction path: every byte of
code is the original source, obtained by structural HTML/JSON parsing only.

Sources (verified):
  - Index:        https://microbit.org/projects/make-it-code-it/
  - Project page: https://microbit.org/projects/make-it-code-it/<slug>/
  - MakeCode API: https://makecode.microbit.org/api/<pubid>/text  (JSON: filename -> contents)

MicroPython comes *only* from the inline code block(s) rendered in each project
page. MakeCode TypeScript comes *only* from the MakeCode share API, resolved via
the project's pub ID, and from `main.ts` only. The two never cross: they are
co-located in one file but kept under distinct `code` keys and never merged.

Output (see Scraper / _write_record):
  - One `<slug>.json` per project. `code` is a map keyed by language
    (`makecode_ts` / `micropython`); a key is present only if that language has
    code. Each language's value is an array of programs: multi-program (e.g.
    radio) projects ship several programs / pub IDs, captured in document order
    and numbered by `program_index`.
  - Each file records its source `url` and (for TS) `pub_id` as an audit trail,
    and carries its `meta` + `context` so the record stands alone.
  - `license` is extracted per page (not assumed): most are CC BY-SA 4.0, but
    some differ (e.g. dance-steps is the NonCommercial CC BY-NC-SA 4.0). A
    missing/unrecognised licence is warned + recorded, never fatal.
  - A pub ID 404 is recorded in that project's `errors` array with a manual URL;
    there is no fallback.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

INDEX_URL = "https://microbit.org/projects/make-it-code-it/"
PROJECT_URL = "https://microbit.org/projects/make-it-code-it/{slug}/"
MAKECODE_TEXT_API = "https://makecode.microbit.org/api/{pubid}/text"
MAKECODE_SHARE = "https://makecode.microbit.org/#pub:{pubid}"

# Each project page states its own licence inline, e.g.
#   "published under a Creative Commons Attribution-ShareAlike 4.0
#    International (CC BY-SA 4.0) licence."
# We extract the bracketed code per page rather than assuming it: most projects
# are CC BY-SA 4.0, but at least one (dance-steps) is the NonCommercial variant
# CC BY-NC-SA 4.0, which is a materially different licence. DEFAULT_LICENSE is
# only a last-resort fallback used (with a warning) if no statement is found.
DEFAULT_LICENSE = "CC BY-SA 4.0"
KNOWN_LICENSES = {"CC BY-SA 4.0", "CC BY-NC-SA 4.0"}
LICENSE_RE = re.compile(
    r"published under (?:a|an)\s+.*?\((CC[^)]*?)\)\s*licen[cs]e",
    re.IGNORECASE | re.DOTALL,
)

# Polite, descriptive identifier with a contact/source pointer.
USER_AGENT = (
    "microbit-corpus-scraper/0.1 (+https://github.com/; "
    "research dataset build; contact: carlospa87@gmail.com)"
)

# Only real "Open in MakeCode" share links. Pub ID is everything after #pub:.
# This deliberately excludes "Open in Classroom" (classroom.microbit.org, which
# also contains the substring "makecode") and "Open in Python" (an encoded blob
# we never use) because neither matches `makecode.microbit.org/#pub:`.
PUB_RE = re.compile(r"makecode\.microbit\.org/#pub:(_[A-Za-z0-9]+)")

# Index slug links: /projects/make-it-code-it/<slug>/
SLUG_RE = re.compile(r"/projects/make-it-code-it/([a-z0-9][a-z0-9-]*)/")

# "123 results" count rendered on the index page.
RESULTS_RE = re.compile(r"(\d+)\s+results", re.IGNORECASE)

LEVELS = ("Beginner", "Intermediate", "Advanced")

# Stable heading ids on each project page (verified by inspecting the HTML).
SUMMARY_ID = "what-is-it?"
HOW_IT_WORKS_ID = "how-it-works"
IMPROVE_IT_ID = "step-3:-improve-it"

RETRY_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 4


# --------------------------------------------------------------------------- #
# HTTP with disk cache + retry/backoff
# --------------------------------------------------------------------------- #

class Fetcher:
    """Fetches URLs with a disk cache and transient-error backoff.

    The cache makes re-runs idempotent and keeps us off the site. A cache hit
    costs no network request and incurs no polite delay. Permanent failures
    (404) are surfaced as ``status`` so callers can log-and-continue; transient
    failures (timeouts / 5xx) are retried with exponential backoff.
    """

    def __init__(self, cache_dir: Path, delay: float = 0.7):
        self.cache_dir = cache_dir
        self.delay = delay
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.network_requests = 0

    def get(self, url: str, cache_path: Path) -> tuple[Optional[str], int]:
        """Return (text, status). status is the HTTP code, or 0 on cache hit,
        or -1 if all retries were exhausted on a transient error."""
        if cache_path.exists():
            return cache_path.read_text(encoding="utf-8"), 0

        backoff = 1.0
        last_status = -1
        for attempt in range(1, MAX_RETRIES + 1):
            if self.delay:
                time.sleep(self.delay)
            try:
                self.network_requests += 1
                resp = self.session.get(url, timeout=30)
            except requests.RequestException as exc:  # transient: connection/timeout
                print(f"    ! network error ({exc.__class__.__name__}) on {url} "
                      f"[attempt {attempt}/{MAX_RETRIES}]", file=sys.stderr)
                time.sleep(backoff)
                backoff *= 2
                continue

            status = resp.status_code
            last_status = status
            if status == 200:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(resp.text, encoding="utf-8")
                return resp.text, status
            if status == 404:
                return None, 404  # permanent: caller logs and continues
            if status in RETRY_STATUS:
                print(f"    ! HTTP {status} on {url} "
                      f"[attempt {attempt}/{MAX_RETRIES}], backing off {backoff:.0f}s",
                      file=sys.stderr)
                time.sleep(backoff)
                backoff *= 2
                continue
            # Any other status: treat as permanent for this run.
            return None, status

        return None, last_status


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

def _norm(text: str) -> str:
    """Collapse runs of whitespace and strip ends (for metadata/prose only)."""
    return re.sub(r"\s+", " ", text).strip()


def parse_index(html: str) -> tuple[list[str], Optional[int]]:
    """Return (ordered unique slugs, listed_count_or_None)."""
    soup = BeautifulSoup(html, "html.parser")
    slugs: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        m = SLUG_RE.search(a["href"])
        if m:
            slug = m.group(1)
            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)
    m = RESULTS_RE.search(soup.get_text(" "))
    listed = int(m.group(1)) if m else None
    return slugs, listed


def _is_linenumber_span(tag: Tag) -> bool:
    if tag.name != "span":
        return False
    cls = tag.get("class")
    return bool(cls) and "linenumber" in cls


def extract_micropython_blocks(soup: BeautifulSoup) -> list[str]:
    """Extract inline code blocks, byte-exact, in document order.

    Each block is a ``<pre><code>...</code></pre>`` with a line-number gutter
    rendered as ``<span class="... linenumber ...">N</span>`` elements. We remove
    those gutter spans *structurally* (never with a regex that could eat
    legitimate leading digits in the code) and then concatenate the remaining
    text nodes. BeautifulSoup has already decoded HTML entities, so the result
    is the original source exactly as written.
    """
    blocks: list[str] = []
    for pre in soup.find_all("pre"):
        code = pre.find("code") or pre
        for span in code.find_all(_is_linenumber_span):
            span.decompose()
        blocks.append(code.get_text())
    return blocks


def extract_pub_ids(soup: BeautifulSoup) -> list[str]:
    """All MakeCode share pub IDs, deduped, in document order."""
    pubs: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        m = PUB_RE.search(a["href"])
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            pubs.append(m.group(1))
    return pubs


# The metadata line: "LEVEL | tools | features | topics", with the literal
# "|" and "," rendered as text nodes between <span>s. It always begins with the
# level word. We match the element whose normalised text *starts* with a level
# word immediately followed by "|".
META_LINE_RE = re.compile(r"^(" + "|".join(LEVELS) + r")\s*\|")


def parse_meta(soup: BeautifulSoup) -> dict:
    """Parse the metadata line: level | tools | features | topics.

    Rendered as ``<span>Beginner</span> | <span>MakeCode</span>, <span>Python</span>
    | <span>LED display</span> | <span>Animation</span>, ...`` where the literal
    ``|`` and ``,`` are text nodes. We find the *tightest* element whose text
    starts with ``LEVEL |`` (shortest matching text), then split on ``|``
    (groups) and ``,`` (items within a group).

    Anchoring on the ``LEVEL |`` pattern — rather than searching for a bare level
    word — is deliberate: a page's "You may also like" cards also contain level
    words (e.g. a Beginner related project linked from an Advanced page), and
    those have no ``|`` separators, so they cannot be mistaken for this line.
    Taking the shortest match avoids sweeping up following prose.
    """
    meta = {"level": "", "tools": [], "features": [], "topics": []}

    best_text: Optional[str] = None
    for el in soup.find_all(True):
        text = _norm(el.get_text())
        if META_LINE_RE.match(text):
            if best_text is None or len(text) < len(best_text):
                best_text = text
    if best_text is None:
        return meta

    groups = [[_norm(t) for t in part.split(",") if _norm(t)]
              for part in best_text.split("|")]
    groups = [g for g in groups if g]  # drop empty trailing/leading groups

    if len(groups) >= 1 and groups[0]:
        meta["level"] = groups[0][0]
    if len(groups) >= 2:
        meta["tools"] = groups[1]
    if len(groups) >= 3:
        meta["features"] = groups[2]
    if len(groups) >= 4:
        meta["topics"] = groups[3]
    return meta


def _section_content(soup: BeautifulSoup, heading_id: str) -> tuple[list[str], list[str]]:
    """Return (paragraph_texts, bullet_texts) for the section that starts at the
    heading with ``heading_id``, stopping at the next heading."""
    heading = soup.find(id=heading_id)
    if heading is None:
        return [], []
    stop = {"h1", "h2", "h3", "h4"}
    paragraphs: list[str] = []
    bullets: list[str] = []
    for sib in heading.next_siblings:
        if isinstance(sib, NavigableString):
            continue
        if not isinstance(sib, Tag):
            continue
        if sib.name in stop:
            break
        # No get_text separator: inline elements (links, <strong>) are embedded
        # in prose whose surrounding text nodes already carry the real spaces.
        # A separator would inject spurious spaces before punctuation
        # (e.g. "LED display ."). _norm then collapses any incidental whitespace.
        if sib.name == "p":
            paragraphs.append(_norm(sib.get_text()))
        elif sib.name in ("ul", "ol"):
            for li in sib.find_all("li"):
                bullets.append(_norm(li.get_text()))
        else:
            for p in sib.find_all("p"):
                paragraphs.append(_norm(p.get_text()))
            for li in sib.find_all("li"):
                bullets.append(_norm(li.get_text()))
    paragraphs = [p for p in paragraphs if p]
    bullets = [b for b in bullets if b]
    return paragraphs, bullets


def parse_context(soup: BeautifulSoup) -> dict:
    """Extract the prose used by the later task-writing stage."""
    summary_ps, _ = _section_content(soup, SUMMARY_ID)
    _, how_bullets = _section_content(soup, HOW_IT_WORKS_ID)
    _, improve_bullets = _section_content(soup, IMPROVE_IT_ID)
    return {
        "summary": summary_ps[0] if summary_ps else "",
        "how_it_works": how_bullets,
        "improve_it": improve_bullets,
    }


def parse_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    return _norm(h1.get_text(" ")) if h1 else ""


def parse_license(soup: BeautifulSoup) -> tuple[str, Optional[str]]:
    """Extract the licence code stated inline on the page.

    Returns ``(code, warning)``. ``warning`` is ``None`` when a recognised code
    was found, ``"no_license_statement"`` when none was present (and we fell back
    to :data:`DEFAULT_LICENSE`), or ``"unexpected_license:<code>"`` when a code
    was found that isn't in :data:`KNOWN_LICENSES` (kept verbatim, but flagged).
    """
    m = LICENSE_RE.search(soup.get_text(" "))
    if not m:
        return DEFAULT_LICENSE, "no_license_statement"
    code = _norm(m.group(1))
    if code not in KNOWN_LICENSES:
        return code, f"unexpected_license:{code}"
    return code, None


# --------------------------------------------------------------------------- #
# Per-project scrape
# --------------------------------------------------------------------------- #

class Scraper:
    def __init__(self, fetcher: Fetcher, out_dir: Path, force: bool = False):
        self.fetcher = fetcher
        self.out_dir = out_dir
        self.force = force

    def fetch_main_ts(self, pubid: str) -> tuple[Optional[str], dict, Optional[dict]]:
        """Return (main_ts_source, dependencies, error_dict).

        Only `main.ts` is taken as *code* (`main.py`, `main.blocks` are ignored).
        The project's `pxt.json` `dependencies` map IS extracted — not as code,
        but as provenance: it is the authoritative list of MakeCode extensions
        the program needs to compile (e.g. ``datalogger``, or a github extension
        such as ``github:microbit-foundation/pxt-sound-level-db``). Stage-3
        verification builds against exactly these; a referenced-but-undeclared
        extension is a genuine build failure, not something to guess around.
        """
        url = MAKECODE_TEXT_API.format(pubid=pubid)
        cache_path = self.fetcher.cache_dir / "api" / f"{pubid}.json"
        text, status = self.fetcher.get(url, cache_path)
        if status == 404:
            return None, {}, {
                "type": "pubid_404",
                "pub_id": pubid,
                "manual_url": MAKECODE_SHARE.format(pubid=pubid),
            }
        if text is None:
            return None, {}, {
                "type": "pubid_fetch_failed",
                "pub_id": pubid,
                "http_status": status,
                "manual_url": MAKECODE_SHARE.format(pubid=pubid),
            }
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None, {}, {"type": "pubid_bad_json", "pub_id": pubid}
        main_ts = data.get("main.ts")
        if main_ts is None:
            return None, {}, {"type": "pubid_no_main_ts", "pub_id": pubid}
        # Extract the dependency map verbatim (deterministic provenance). A
        # missing/unparseable pxt.json yields no deps rather than an error: the
        # code is still valid; verification simply uses the core defaults.
        dependencies: dict = {}
        pxt_raw = data.get("pxt.json")
        if pxt_raw:
            try:
                dependencies = json.loads(pxt_raw).get("dependencies", {}) or {}
            except (json.JSONDecodeError, AttributeError):
                dependencies = {}
        return main_ts, dependencies, None

    def scrape(self, slug: str, prior: Optional[dict]) -> Optional[dict]:
        """Scrape one project. Returns its manifest record (or None on a
        permanent page failure, which is logged and skipped)."""
        url = PROJECT_URL.format(slug=slug)

        # Resume: skip if a prior record exists and its expected output file is
        # present on disk. A project gets a file when it has code in either
        # language or any TS error worth auditing; a code-less, error-less
        # project has no file, so this resumes those (vacuously true) too.
        if prior is not None and not self.force:
            expects_file = (prior.get("has_makecode_ts")
                            or prior.get("has_micropython")
                            or prior.get("error_count", 0) > 0)
            if not expects_file or (self.out_dir / f"{slug}.json").exists():
                print(f"  = {slug}: up-to-date, skipping")
                return prior

        cache_path = self.fetcher.cache_dir / "pages" / f"{slug}.html"
        html, status = self.fetcher.get(url, cache_path)
        if html is None:
            print(f"  x {slug}: page fetch failed (status {status}) — skipping",
                  file=sys.stderr)
            return {
                "slug": slug, "has_makecode_ts": False, "has_micropython": False,
                "pub_ids": [], "license": None, "license_warning": "page_fetch_failed",
                "error_count": 1,
            }

        soup = BeautifulSoup(html, "html.parser")
        title = parse_title(soup)
        meta = parse_meta(soup)
        context = parse_context(soup)
        license_code, license_warning = parse_license(soup)
        if license_warning:
            print(f"    ! {slug}: licence {license_warning} "
                  f"(recorded as {license_code!r})", file=sys.stderr)
        py_blocks = extract_micropython_blocks(soup)
        pub_ids = extract_pub_ids(soup)

        # --- MakeCode TypeScript (from /text API, main.ts only) ---------------
        ts_code: list[dict] = []
        ts_errors: list[dict] = []
        for pubid in pub_ids:
            main_ts, dependencies, err = self.fetch_main_ts(pubid)
            if err is not None:
                ts_errors.append(err)
                continue
            ts_code.append({
                "program_index": len(ts_code),
                "pub_id": pubid,
                "source": main_ts,
                "dependencies": dependencies,
            })

        # --- MicroPython (from inline page block(s)) --------------------------
        py_code: list[dict] = []
        for src in py_blocks:
            py_code.append({"program_index": len(py_code), "source": src})

        has_ts = bool(ts_code)
        has_py = bool(py_code)

        # Write one merged file if we captured code in either language, or if we
        # have TS errors worth keeping an audit trail for (pub IDs that failed to
        # resolve). A code key is present only when that language has blocks.
        if has_ts or has_py or ts_errors:
            code: dict[str, list[dict]] = {}
            if has_ts:
                code["makecode_ts"] = ts_code
            if has_py:
                code["micropython"] = py_code
            self._write_record(slug, url, title, license_code, meta, context,
                               code, ts_errors)

        flags = []
        if has_ts:
            flags.append("makecode_ts")
        if has_py:
            flags.append("micropython")
        label = "+".join(flags) if flags else "no-code"
        err_note = f" ({len(ts_errors)} error(s))" if ts_errors else ""
        print(f"  + {slug}: {label}{err_note} "
              f"[py_blocks={len(py_code)}, pub_ids={len(pub_ids)}]")

        return {
            "slug": slug,
            "has_makecode_ts": has_ts,
            "has_micropython": has_py,
            "pub_ids": pub_ids,
            "license": license_code,
            "license_warning": license_warning,
            "error_count": len(ts_errors),
        }

    def _write_record(self, slug: str, url: str, title: str,
                      license_code: str, meta: dict, context: dict,
                      code: dict[str, list[dict]], errors: list[dict]) -> None:
        # `code` is a map keyed by language (makecode_ts / micropython); both
        # languages live in one file but stay under distinct keys — never merged.
        record = {
            "slug": slug,
            "url": url,
            "title": title,
            "license": license_code,
            "meta": meta,
            "context": context,
            "code": code,
            "errors": errors,
        }
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{slug}.json"
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #

def build_manifest(index_url: str, listed: Optional[int],
                   records: list[dict]) -> dict:
    from collections import Counter

    summary = {
        "with_both": 0, "makecode_ts_only": 0, "micropython_only": 0,
        "no_code": 0, "with_errors": 0, "license_warnings": 0,
    }
    licenses: Counter = Counter()
    for r in records:
        ts, py = r["has_makecode_ts"], r["has_micropython"]
        if ts and py:
            summary["with_both"] += 1
        elif ts:
            summary["makecode_ts_only"] += 1
        elif py:
            summary["micropython_only"] += 1
        else:
            summary["no_code"] += 1
        if r["error_count"] > 0:
            summary["with_errors"] += 1
        if r.get("license_warning"):
            summary["license_warnings"] += 1
        # Count every licence encountered across all scraped pages.
        licenses[r.get("license") or "(none)"] += 1
    # Sorted most-common-first for a stable, readable breakdown.
    summary["licenses"] = dict(sorted(
        licenses.items(), key=lambda kv: (-kv[1], kv[0])))
    return {
        "scraped_at": _dt.datetime.now(_dt.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "index_url": index_url,
        "projects_listed": listed if listed is not None else len(records),
        "projects_scraped": len(records),
        "projects": records,
        "summary": summary,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("data/collected"),
                        help="Output directory (default: data/collected). Manifest "
                             "is written to <out>/manifest.json.")
    parser.add_argument("--cache", type=Path, default=Path("cache/scrape"),
                        help="Cache directory for raw HTML and API JSON "
                             "(default: cache/scrape).")
    parser.add_argument("--delay", type=float, default=0.7,
                        help="Seconds to wait before each network request "
                             "(cache hits incur no delay).")
    parser.add_argument("--force", action="store_true",
                        help="Re-process and overwrite outputs even if present.")
    parser.add_argument("--only", nargs="*", default=None,
                        help="Only process these explicit slugs (e.g. to "
                             "re-scrape a single project with --force).")
    args = parser.parse_args(argv)

    fetcher = Fetcher(args.cache, delay=args.delay)
    scraper = Scraper(fetcher, args.out, force=args.force)

    # Load any prior manifest for resumability.
    manifest_path = args.out / "manifest.json"
    prior_by_slug: dict[str, dict] = {}
    if manifest_path.exists():
        try:
            prior = json.loads(manifest_path.read_text(encoding="utf-8"))
            for r in prior.get("projects", []):
                prior_by_slug[r["slug"]] = r
        except (json.JSONDecodeError, KeyError):
            print("  ! existing manifest unreadable; ignoring", file=sys.stderr)

    print(f"Fetching index: {INDEX_URL}")
    index_html, status = fetcher.get(INDEX_URL, args.cache / "index.html")
    if index_html is None:
        print(f"FATAL: could not fetch index (status {status})", file=sys.stderr)
        return 1

    index_slugs, listed = parse_index(index_html)
    print(f"Found {len(index_slugs)} unique slugs; index reports "
          f"{listed if listed is not None else '?'} results.")
    if listed is not None and listed != len(index_slugs):
        print(f"  ! WARNING: slug count ({len(index_slugs)}) != listed count "
              f"({listed}). Continuing anyway (possible lazy-load or layout "
              f"change).", file=sys.stderr)

    to_process = index_slugs
    if args.only:
        to_process = [s for s in index_slugs if s in set(args.only)]
        print(f"  (--only) restricted to {len(to_process)} slug(s): {to_process}")

    processed: dict[str, dict] = {}
    for i, slug in enumerate(to_process, 1):
        print(f"[{i}/{len(to_process)}] {slug}")
        rec = scraper.scrape(slug, prior_by_slug.get(slug))
        if rec is not None:
            processed[slug] = rec

    # Assemble the manifest over the full index in order, using this run's
    # freshly-scraped records where present and falling back to the prior
    # manifest otherwise. This keeps a partial (--only) run from dropping the
    # projects it didn't touch.
    records: list[dict] = []
    for slug in index_slugs:
        rec = processed.get(slug) or prior_by_slug.get(slug)
        if rec is not None:
            records.append(rec)

    manifest = build_manifest(INDEX_URL, listed, records)
    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    s = manifest["summary"]
    print("\n=== Summary ===")
    print(f"  index listed:     {manifest['projects_listed']}")
    print(f"  projects scraped: {manifest['projects_scraped']}")
    print(f"  with both:        {s['with_both']}")
    print(f"  makecode_ts only: {s['makecode_ts_only']}")
    print(f"  micropython only: {s['micropython_only']}")
    print(f"  no code:          {s['no_code']}")
    print(f"  with errors:      {s['with_errors']}")
    print(f"  network requests: {fetcher.network_requests}")
    print("  licenses encountered:")
    for code, n in s["licenses"].items():
        print(f"      {n:>4}  {code}")
    if s["license_warnings"]:
        print(f"  ! licence warnings: {s['license_warnings']} "
              f"(see per-project 'license_warning' in manifest)")
    print(f"  manifest:         {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
