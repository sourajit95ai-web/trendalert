#!/usr/bin/env python3
"""Build a frozen, write-blocked replica of the dashboard for outside review.

Handing someone the live URL has two problems:

  1. The dashboard writes home. `publish()` POSTs `universe` and `core` three
     seconds after ANY page load, and saving Settings POSTs `settings` -- which
     the backend reads for alert thresholds. A reviewer nudging a score weight
     or a zone percentage would change what the morning brief and bloodbath
     alerts fire on.
  2. The data moves under them. Feedback gathered against a shifting dashboard
     is hard to act on, and reviewers report stale prices as bugs.

So this snapshots data.json and every chart/<sym>.json into the page and
installs a fetch shim that answers them locally, refuses every non-GET, and
blocks the notes endpoint outright. The result is one self-contained file: no
network calls, no writes home, same UI.

The shim matches on URL rather than origin, which matters -- the copy is
usually served from storage.googleapis.com, the same origin as the real
data.json, so an origin-based guard would let same-origin requests straight
through.

The page it produces is titled "TrendAlert demo" -- it is what gets circulated
for feedback, so it is named for the reader, not for the build step.

SOURCE: it builds from frontend/dashboard-next.html, the canonical dashboard.
It used to build from frontend/dashboard.html; that artifact was frozen on
2026-08-15 and now drifts from its source, so a demo cut from it would show
an old UI. If the dashboard ever moves again, this path moves with it.

Usage
-----
    python frontend/make_review_copy.py [out.html]

Publishing it for others to open (public and unauthenticated, so the link is
the only barrier -- the file has the portfolio baked in):

    gcloud storage cp demo.html \\
      gs://trendalert-data-rattle/demo/dashboard.html \\
      --gzip-local-all --cache-control="no-cache" --content-type="text/html"

gzip matters here: megabytes on disk become a few hundred KB on the wire. Note
gsutil is broken in this environment (its python shim errors), hence
`gcloud storage`.
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

BUCKET = "https://storage.googleapis.com/trendalert-data-rattle"
HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "dashboard-next.html"
OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "demo.html")


def grab(name: str) -> dict:
    """Fetch a bucket object, cache-busted -- the public URL is CDN-cached."""
    with urllib.request.urlopen(f"{BUCKET}/{name}?t=review", timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def grab_charts(symbols) -> dict:
    """Per-symbol bars -> {object_name: bars}, keyed as the page will ask.

    Bars moved to one object per symbol, fetched on demand. The live page
    therefore never requests chart.json, so a copy that snapshots only
    chart.json shows empty charts for every name. Fetch each one and key the
    snapshot by the same object path the shim matches on.

    A symbol whose object is missing is skipped rather than fatal: the copy is
    for review, and one dead chart is better than no copy at all.
    """
    out, missing = {}, []
    for sym in symbols:
        name = f"chart/{str(sym).replace('/', '-')}.json"
        try:
            out[name] = grab(name)
        except Exception:
            missing.append(sym)
    if missing:
        print(f"  no bars for {len(missing)}: {', '.join(missing[:8])}"
              + (" ..." if len(missing) > 8 else ""))
    return out


def embed(obj) -> str:
    """JSON safe to sit inside a <script> block."""
    return json.dumps(obj, separators=(",", ":")).replace("<", "\\u003c")


def build() -> None:
    if not SRC.is_file():
        sys.exit(f"make_review_copy: {SRC} not found -- run build_next.py first")

    data = grab("data.json")
    charts = grab_charts([r.get("symbol") for r in data.get("symbols", [])
                          if r.get("symbol")])
    stamp = data.get("generated_at") or data.get("as_of") or "unknown"

    shim = f"""<script>
/* ---- REVIEW COPY -------------------------------------------------------
   Frozen snapshot. Every fetch is answered locally; nothing leaves the page.
   Generated from data.json stamped {stamp}
   ---------------------------------------------------------------------- */
(function () {{
  var DATA = {embed(data)};
  var CHARTS = {embed(charts)};
  var STAMP = {embed(stamp)};
  var reply = function (obj) {{
    return Promise.resolve(new Response(JSON.stringify(obj), {{
      status: 200, headers: {{ "Content-Type": "application/json" }}
    }}));
  }};
  var real = window.fetch;
  window.fetch = function (input, init) {{
    var url = String((input && input.url) || input || "");
    var method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
    if (method !== "GET") return reply({{}});             // never write anywhere
    if (url.indexOf("/notes") !== -1) return reply({{}});   // no sync pull either
    // per-symbol bars: match on the chart/<name>.json tail, ignoring the
    // cache-buster the page appends
    var m = url.match(/chart\\/[^/?]+\\.json/);
    if (m) return reply(CHARTS[m[0]] || []);
    if (url.indexOf("data.json") !== -1) return reply(DATA);
    return real.apply(this, arguments);
  }};

  document.addEventListener("DOMContentLoaded", function () {{
    var bar = document.createElement("div");
    bar.setAttribute("role", "note");
    bar.style.cssText = [
      "position:fixed", "left:14px", "bottom:14px", "z-index:9999",
      "display:flex", "align-items:center", "gap:10px",
      "font:500 12px/1.4 Inter, system-ui, sans-serif",
      "padding:9px 13px", "border-radius:8px",
      "color:var(--color-text, #e9e9ed)",
      "background:var(--color-surface, #232532)",
      "border:1px solid var(--color-neutral-800, #3f424d)",
      "box-shadow:0 6px 24px rgba(0,0,0,.28)", "max-width:min(92vw,460px)"
    ].join(";");
    var label = document.createElement("span");
    label.textContent = "TrendAlert demo — frozen data, nothing is saved or sent. Snapshot: " +
      String(STAMP).slice(0, 16).replace("T", " ") + " UTC";
    var x = document.createElement("button");
    x.textContent = "Dismiss";
    x.setAttribute("aria-label", "Dismiss review notice");
    x.style.cssText = [
      "font:500 12px/1 Inter, system-ui, sans-serif", "cursor:pointer",
      "color:var(--color-neutral-400, #b2b6ca)", "background:transparent",
      "border:1px solid var(--color-neutral-800, #3f424d)",
      "border-radius:6px", "padding:5px 9px", "flex:none"
    ].join(";");
    x.onclick = function () {{ bar.remove(); }};
    bar.appendChild(label); bar.appendChild(x);
    document.body.appendChild(bar);
  }});
}})();
</script>
"""

    html = SRC.read_text(encoding="utf-8")
    marker = "<head>\n"
    if marker not in html:
        sys.exit("make_review_copy: could not find <head> to inject before")
    # inject first, so fetch is patched before React or the runtime evaluate
    html = html.replace(marker, marker + shim, 1)
    # the browser tab is the first thing a reviewer sees -- name the page for
    # them, and assert rather than silently shipping the plain product title
    if "<title>TrendAlert</title>" not in html:
        sys.exit("make_review_copy: <title>TrendAlert</title> not found -- "
                 "the source title changed, update this replacement")
    html = html.replace("<title>TrendAlert</title>",
                        "<title>TrendAlert demo</title>", 1)

    OUT.write_text(html, encoding="utf-8", newline="")
    print(f"make_review_copy: wrote {OUT.name} ({OUT.stat().st_size / 1024:,.0f} KB) — snapshot {stamp}")
    print(f"  {len(data.get('symbols', []))} symbols, {len(charts)} chart series")


if __name__ == "__main__":
    build()
