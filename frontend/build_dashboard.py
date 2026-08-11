#!/usr/bin/env python3
"""Build frontend/dashboard.html from the frontend/v2/ source tree.

The dashboard is authored as a source tree (template + runtime + design
tokens + vendored libs) but deployed as ONE self-contained GCS object, the same
way dashboard.html always has been. This script does that inlining.

Three rules make or break the output:

  1. support.js must land AFTER </x-dc>, not in <head>. The runtime locates the
     component template by searching the document source for the first <x-dc>.
     support.js contains that literal string in its own code, so inlining it
     ahead of the template makes the runtime match itself and render its own
     source as text.

  2. React must be defined before support.js runs, or the runtime falls back to
     fetching React from unpkg -- which breaks the "no CDN" guarantee and fails
     entirely offline. react.js/react-dom.js stay in <head>.

  3. No <script> may end up INSIDE <x-dc>. support.js runs the template source
     through encodeCase(), whose CAMEL_ATTR_RE rewrites `camelCase =` into
     `sc-camel-camel-case =` so the HTML parser cannot lowercase an attribute
     name like onClick. It is applied to the whole template string, script
     bodies included, so a script inlined inside the template gets its
     JavaScript identifiers mangled: `const lsGet =` became
     `const sc-camel-ls-get =`, and the helmet mounter then appended that to
     <head> and ran it -- "Uncaught SyntaxError: Missing initializer in const
     declaration" on every page load since the v2 port. The page still worked
     because the browser had already executed the same scripts correctly while
     parsing the template; the mangled copy was a second, doomed run.
     So <helmet>'s scripts are inlined into <head> instead, which also stops
     ~205KB of vendored JS being parsed and executed twice. build() asserts the
     rule rather than trusting it.

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
OPEN_TEMPLATE = "<x-dc>"
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
    if OPEN_TEMPLATE not in doc:
        sys.exit(f"build_dashboard: {ENTRY.name} has no {OPEN_TEMPLATE} -- not a Design Component.")

    # Split at the template so the two halves can be inlined under different
    # rules: scripts are allowed to stay put in <head>, but any inside <x-dc>
    # have to be hoisted out of it (rule 3).
    before, _, rest = doc.partition(OPEN_TEMPLATE)
    template, _, after = rest.partition(CLOSE_TEMPLATE + "\n")

    deferred_js: list[str] = []
    hoisted_js: list[str] = []

    def inline_head_script(m: re.Match[str]) -> str:
        href = m.group("href")
        if href.endswith(DEFERRED):
            deferred_js.append(escape_close_tags(read(href)))
            return ""  # re-emitted after </x-dc>
        return f"<script>\n{escape_close_tags(read(href))}\n</script>\n"

    def hoist_template_script(m: re.Match[str]) -> str:
        hoisted_js.append(escape_close_tags(read(m.group("href"))))
        return ""  # re-emitted in <head>, NOT here

    def inline_style(m: re.Match[str]) -> str:
        return f"<style>\n{read(m.group('href'))}\n</style>\n"

    before = SCRIPT_SRC.sub(inline_head_script, before)
    template = SCRIPT_SRC.sub(hoist_template_script, template)
    before = STYLE_HREF.sub(inline_style, before)
    # helmet stylesheets stay in the helmet: that is how the runtime mounts them,
    # and CSS is untouched by the camelCase rewrite, which needs a following `=`
    template = STYLE_HREF.sub(inline_style, template)

    if not deferred_js:
        sys.exit(f"build_dashboard: never found <script src=...{DEFERRED}> to defer.")

    # The hoisted libraries land after react/react-dom and before support.js,
    # which is what first paint needs: the component reads window.TA and
    # window.LightweightCharts as it renders.
    hoisted = "".join(f"<script>\n{js}\n</script>\n" for js in hoisted_js)
    before = before.replace("</head>\n", hoisted + "</head>\n", 1)

    runtime = "".join(f"<script>\n{js}\n</script>\n" for js in deferred_js)
    out = before + OPEN_TEMPLATE + template + CLOSE_TEMPLATE + "\n" + runtime + after

    # Rule 3, enforced. A <script> reintroduced into the template would be
    # mangled by encodeCase and throw at load, which is easy to miss because the
    # page still renders.
    body = out.partition(OPEN_TEMPLATE)[2].partition(CLOSE_TEMPLATE)[0]
    if "<script" in body:
        sys.exit(
            "build_dashboard: a <script> is inside <x-dc>. support.js's encodeCase()\n"
            "  rewrites `camelCase =` across the whole template, script bodies included,\n"
            "  so its JS identifiers get mangled and it throws when the helmet mounts it.\n"
            "  Move it into <head>, or hoist it the way <helmet>'s scripts are hoisted."
        )
    return out


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
