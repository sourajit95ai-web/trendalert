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

So this snapshots data.json and chart.json into the page and installs a fetch
shim that answers them locally, refuses every non-GET, and blocks the notes
endpoint outright. The result is one self-contained file: no network calls, no
writes home, same UI.

The shim matches on URL rather than origin, which matters -- the copy is
usually served from storage.googleapis.com, the same origin as the real
data.json, so an origin-based guard would let same-origin requests straight
through.

Usage
-----
    python frontend/make_review_copy.py [out.html]

Publishing it for others to open (public and unauthenticated, so the link is
the only barrier -- the file has the portfolio baked in):

    gcloud storage cp review-copy.html \\
      gs://trendalert-data-rattle/review/dashboard.html \\
      --gzip-local-all --cache-control="no-cache" --content-type="text/html"

gzip matters here: ~2.5MB on disk becomes ~680KB on the wire. Note gsutil is
broken in this environment (its python shim errors), hence `gcloud storage`.
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

BUCKET = "https://storage.googleapis.com/trendalert-data-rattle"
HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "dashboard.html"
OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "review-copy.html")


def grab(name: str) -> dict:
    """Fetch a bucket object, cache-busted -- the public URL is CDN-cached."""
    with urllib.request.urlopen(f"{BUCKET}/{name}?t=review", timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def embed(obj) -> str:
    """JSON safe to sit inside a <script> block."""
    return json.dumps(obj, separators=(",", ":")).replace("<", "\\u003c")


def build() -> None:
    if not SRC.is_file():
        sys.exit(f"make_review_copy: {SRC} not found -- run build_dashboard.py first")

    data, chart = grab("data.json"), grab("chart.json")
    stamp = data.get("generated_at") or data.get("as_of") or "unknown"

    shim = f"""<script>
/* ---- REVIEW COPY -------------------------------------------------------
   Frozen snapshot. Every fetch is answered locally; nothing leaves the page.
   Generated from data.json stamped {stamp}
   ---------------------------------------------------------------------- */
(function () {{
  var DATA = {embed(data)};
  var CHART = {embed(chart)};
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
    if (url.indexOf("chart.json") !== -1) return reply(CHART);
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
    label.textContent = "Review copy — frozen data, nothing is saved or sent. Snapshot: " +
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
    html = html.replace("<title>", "<title>TrendAlert — review copy · ", 1)

    OUT.write_text(html, encoding="utf-8", newline="")
    print(f"make_review_copy: wrote {OUT.name} ({OUT.stat().st_size / 1024:,.0f} KB) — snapshot {stamp}")
    print(f"  {len(data.get('symbols', []))} symbols, {len(chart) if isinstance(chart, dict) else '?'} chart series")


if __name__ == "__main__":
    build()
