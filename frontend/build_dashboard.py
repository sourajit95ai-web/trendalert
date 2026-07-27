#!/usr/bin/env python3
"""Build frontend/dashboard.html from the frontend/v2/ source tree.

The dashboard is authored as a source tree (template + runtime + design
tokens + vendored libs) but deployed as ONE self-contained GCS object, the same
way dashboard.html always has been. This script does that inlining.

Two rules make or break the output, both learned from the vendor handoff:

  1. support.js must land AFTER </x-dc>, not in <head>. The runtime locates the
     component template by searching the document source for the first <x-dc>.
     support.js contains that literal string in its own code, so inlining it
     ahead of the template makes the runtime match itself and render its own
     source as text.

  2. React must be defined before support.js runs, or the runtime falls back to
     fetching React from unpkg -- which breaks the "no CDN" guarantee and fails
     entirely offline. react.js/react-dom.js stay in <head>.

Usage:  python frontend/build_dashboard.py [--check]
        --check  build in memory and diff against the committed output
                 instead of writing it (used by CI)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "v2"
ENTRY = SRC_DIR / "TrendAlert.dc.html"
OUT = Path(__file__).resolve().parent / "dashboard.html"

# support.js is pulled out of <head> and re-emitted after </x-dc> (rule 1).
DEFERRED = "support.js"
CLOSE_TEMPLATE = "</x-dc>"

SCRIPT_SRC = re.compile(r'[ \t]*<script src="(?P<href>[^"]+)"></script>\n?')
STYLE_HREF = re.compile(r'[ \t]*<link rel="stylesheet" href="(?P<href>[^"]+)">\n?')


def read(href: str) -> str:
    """Resolve an href relative to the source tree and return its text."""
    path = (SRC_DIR / href.lstrip("./")).resolve()
    if not path.is_file():
        sys.exit(f"build_dashboard: missing asset {href} (looked in {path})")
    return path.read_text(encoding="utf-8")


def escape_close_tags(js: str) -> str:
    """Neutralise any literal </script> inside inlined JS.

    None of the currently vendored files contain one, but a future edit to
    trendalert-core.js easily could, and the failure mode is a truncated
    document rather than a loud error.
    """
    return js.replace("</script>", r"<\/script>")


def build() -> str:
    doc = ENTRY.read_text(encoding="utf-8")

    if CLOSE_TEMPLATE not in doc:
        sys.exit(f"build_dashboard: {ENTRY.name} has no {CLOSE_TEMPLATE} -- not a Design Component.")

    deferred_js: list[str] = []

    def inline_script(m: re.Match[str]) -> str:
        href = m.group("href")
        if href.endswith(DEFERRED):
            deferred_js.append(escape_close_tags(read(href)))
            return ""  # re-emitted after </x-dc>
        return f"<script>\n{escape_close_tags(read(href))}\n</script>\n"

    def inline_style(m: re.Match[str]) -> str:
        return f"<style>\n{read(m.group('href'))}\n</style>\n"

    doc = SCRIPT_SRC.sub(inline_script, doc)
    doc = STYLE_HREF.sub(inline_style, doc)

    if not deferred_js:
        sys.exit(f"build_dashboard: never found <script src=...{DEFERRED}> to defer.")

    runtime = "".join(f"<script>\n{js}\n</script>\n" for js in deferred_js)
    head, sep, tail = doc.partition(CLOSE_TEMPLATE + "\n")
    return head + sep + runtime + tail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify the committed build is current")
    args = ap.parse_args()

    out = build()

    if args.check:
        if not OUT.is_file():
            print(f"build_dashboard: {OUT.name} is missing -- run `python frontend/build_dashboard.py`")
            return 1
        if OUT.read_text(encoding="utf-8") != out:
            print(f"build_dashboard: {OUT.name} is stale -- run `python frontend/build_dashboard.py` and commit")
            return 1
        print(f"build_dashboard: {OUT.name} is up to date ({len(out):,} bytes)")
        return 0

    OUT.write_text(out, encoding="utf-8", newline="")
    print(f"build_dashboard: wrote {OUT.name} ({len(out):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
