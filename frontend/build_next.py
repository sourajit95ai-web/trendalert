#!/usr/bin/env python3
"""Build frontend/dashboard-next.html from the frontend/next/ source tree.

`next` is the redesign in progress. It is published to its OWN object
(gs://BUCKET/next/dashboard.html) and never overwrites the live dashboard --
promotion, when we get there, is a deliberate rename, not a deploy flag.

Same inlining contract as build_dashboard.py: one self-contained HTML file, no
CDN, no external requests. Unlike the v2 tree there is no Design Component
runtime here, so neither of build_dashboard.py's ordering rules applies -- the
page is plain markup plus two scripts, and assets inline where they sit.

Assets are referenced relative to frontend/next/, so `../v2/theme.css` reaches
back into the shipped tree. The redesign deliberately reuses v2's design
tokens, theme layer, vendored Inter and -- most importantly --
trendalert-core.js, which is the DOM-free logic core. Rewriting the UI must
not rewrite the rules.

Usage:  python frontend/build_next.py [--check]
        --check  build in memory and diff against the committed output
                 instead of writing it (used by CI)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "next"
ENTRY = SRC_DIR / "index.html"
OUT = Path(__file__).resolve().parent / "dashboard-next.html"

SCRIPT_SRC = re.compile(r'[ \t]*<script src="(?P<href>[^"]+)"></script>\n?')
STYLE_HREF = re.compile(r'[ \t]*<link rel="stylesheet" href="(?P<href>[^"]+)">\n?')


def read(href: str) -> str:
    """Resolve an href relative to the source tree and return its text."""
    path = (SRC_DIR / href).resolve()
    if not path.is_file():
        sys.exit(f"build_next: missing asset {href} (looked in {path})")
    return path.read_text(encoding="utf-8")


def escape_close_tags(js: str) -> str:
    """Neutralise any literal </script> inside inlined JS -- otherwise the
    document truncates silently rather than failing loudly."""
    return js.replace("</script>", r"<\/script>")


def build() -> str:
    doc = ENTRY.read_text(encoding="utf-8")
    doc = SCRIPT_SRC.sub(lambda m: f"<script>\n{escape_close_tags(read(m.group('href')))}\n</script>\n", doc)
    doc = STYLE_HREF.sub(lambda m: f"<style>\n{read(m.group('href'))}\n</style>\n", doc)
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify the committed build is current")
    args = ap.parse_args()

    out = build()

    if args.check:
        if not OUT.is_file():
            print(f"build_next: {OUT.name} is missing -- run `python frontend/build_next.py`")
            return 1
        if OUT.read_text(encoding="utf-8") != out:
            print(f"build_next: {OUT.name} is stale -- run `python frontend/build_next.py` and commit")
            return 1
        print(f"build_next: {OUT.name} is up to date ({len(out):,} bytes)")
        return 0

    OUT.write_text(out, encoding="utf-8", newline="")
    print(f"build_next: wrote {OUT.name} ({len(out):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
