"""
notes_function.py — HTTP Cloud Function persisting notes AND positions to GCS.

Stores in one function, selected by the `kind` param:
  kind=notes     -> notes.json      { "AAPL": [ {text, date}, ... ] }
  kind=positions -> positions.json  { "AAPL": {entry, date, booked, bookedDate}, ... }
  kind=settings  -> settings.json   scoring weights / thresholds / alert channel
  kind=universe  -> universe.json   [ "AAPL", ... ] — union of dashboard list
  kind=pbre      -> pbre.json       { "AAPL": {pb:1, re:1}, ... } — booked/re-entered markers
                    symbols; the pipeline merges it into its fetch universe
                    so UI-added tickers get data on the next cycle

AUTH: GET is open (it returns what is already public in the bucket). Every
POST must carry `X-TA-Token` matching the ADMIN_TOKEN env var, wired from
Secret Manager at deploy time. Without it the function is read-only, so the
dashboard can be handed to anyone without them being able to change lists,
settings or alert thresholds. It is ONE shared password, not per-user auth:
anyone holding it has full write access, and revoking means rotating the
secret and re-entering it wherever you use the dashboard.

GET  ?kind=positions              -> the whole positions map (dashboard loads once)
GET  ?kind=notes&symbol=AAPL      -> notes for one symbol
POST {kind:"positions", data:{...whole map...}}         -> overwrite positions.json
POST {kind:"notes", symbol:"AAPL", notes:[...]}          -> overwrite one symbol's notes

Deploy (Gen2, Python):
    gcloud functions deploy notes \
      --gen2 --runtime python312 --region us-central1 \
      --trigger-http --allow-unauthenticated \
      --set-env-vars GCS_BUCKET=YOUR_BUCKET \
      --set-secrets ADMIN_TOKEN=trendalert-admin-token:latest \
      --entry-point notes

requirements.txt:
    functions-framework
    google-cloud-storage

Concurrency: last-write-wins (single-user tool; acceptable, same as data.json).
"""

import hmac
import json
import os
import re
from google.cloud import storage

BUCKET = os.environ.get("GCS_BUCKET", "")
TOKEN_HEADER = "X-TA-Token"
OBJECTS = {"notes": "notes.json", "positions": "positions.json",
           "settings": "settings.json", "universe": "universe.json",
           "pbre": "pbre.json", "core": "core.json"}
_SYM_RE = re.compile(r"^[A-Z0-9./-]{1,16}$")

_client = None


def _blob(kind):
    global _client
    if _client is None:            # lazy so importing the module needs no creds
        _client = storage.Client()
    return _client.bucket(BUCKET).blob(OBJECTS[kind])


def _read(kind):
    b = _blob(kind)
    if not b.exists():
        return {}
    try:
        return json.loads(b.download_as_text() or "{}")
    except json.JSONDecodeError:
        return {}


def _write(kind, data):
    blob = _blob(kind)
    blob.cache_control = "no-cache, max-age=0"   # keep public reads fresh (matches data.json)
    blob.upload_from_string(
        json.dumps(data, ensure_ascii=False), content_type="application/json"
    )


def merge_universe(existing, incoming, cap=250):
    """Order-preserving union of two symbol lists, sanitized and capped.

    ADDITIVE by design: `incoming` (whatever the currently-loaded browser has
    in localStorage) can only ADD symbols, never remove them. A second device,
    a fresh/cleared session, or an automated load whose list lacks a UI-added
    ticker can therefore no longer wipe it from the pipeline's fetch universe.
    Stale symbols are harmless — the pipeline caps extras (MAX_EXTRA_*) and the
    dashboard only shows symbols that are actually in a list."""
    out, seen = [], set()
    src = (existing if isinstance(existing, list) else []) \
        + (incoming if isinstance(incoming, list) else [])
    for s in src:
        if len(out) >= cap:
            break
        s = str(s).strip().upper()[:16]
        if s and _SYM_RE.match(s) and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# CORS: locked to the dashboard's serving origin.
# Set ALLOWED_ORIGIN env var at deploy time; GCS-bucket serving uses
# https://storage.googleapis.com (the origin excludes bucket/path).
# Leave unset only for local file:// testing (falls back to *).
_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
_CORS = {
    "Access-Control-Allow-Origin": _ORIGIN,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    # TOKEN_HEADER must be advertised here or the browser's preflight rejects
    # the write before it is ever sent.
    "Access-Control-Allow-Headers": f"Content-Type, {TOKEN_HEADER}",
    "Vary": "Origin",
}
_JSON = {**_CORS, "Content-Type": "application/json"}


def _authorized(request):
    """True if this request may WRITE.

    Reads ADMIN_TOKEN from the environment on every call rather than at import
    so a redeploy that rotates the secret takes effect without a cold start
    (and so tests can set it per-case).

    Fails CLOSED: with no ADMIN_TOKEN configured nothing can write. That is the
    opposite of the alert switches' fail-open default, and deliberately so — a
    misconfigured deploy should cost the owner a save, not leave the endpoint
    open to the internet. GET is unaffected; the data it returns is already
    public in the bucket.
    """
    # .strip() both sides: `echo secret | gcloud secrets create` stores a
    # trailing newline, which would otherwise reject the correct password with
    # an indistinguishable 401. The browser trims what it sends too, so a
    # password can never meaningfully start or end with whitespace anyway.
    expected = os.environ.get("ADMIN_TOKEN", "").strip()
    if not expected:
        return False
    got = str(request.headers.get(TOKEN_HEADER, "") or "").strip()
    # compare_digest to keep the comparison time-independent of how many
    # leading characters a guess got right
    return bool(got) and hmac.compare_digest(got, expected)


def notes(request):
    if request.method == "OPTIONS":
        return ("", 204, _CORS)

    kind = (request.args.get("kind") or
            (request.get_json(silent=True) or {}).get("kind") or "notes")
    if kind not in OBJECTS:
        return (json.dumps({"error": "kind must be notes|positions|settings|universe|pbre|core"}), 400, _JSON)

    if request.method == "GET":
        data = _read(kind)
        if kind == "notes":
            symbol = request.args.get("symbol", "")
            return (json.dumps({"symbol": symbol, "notes": data.get(symbol, [])}), 200, _JSON)
        if kind == "settings":
            return (json.dumps({"settings": data}), 200, _JSON)
        if kind in ("universe", "core"):
            return (json.dumps({kind: data if isinstance(data, list) else []}), 200, _JSON)
        if kind == "pbre":
            return (json.dumps({"pbre": data}), 200, _JSON)
        return (json.dumps({"positions": data}), 200, _JSON)

    if request.method == "POST":
        # Gate FIRST: before the payload is parsed, before any bucket read or
        # write. Every kind below is a write, so there is no unauthenticated
        # POST path left.
        if not _authorized(request):
            if not os.environ.get("ADMIN_TOKEN", "").strip():
                return (json.dumps({"error": "writes disabled: ADMIN_TOKEN not configured"}),
                        503, _JSON)
            return (json.dumps({"error": "unauthorized"}), 401, _JSON)

        payload = request.get_json(silent=True) or {}

        if kind in ("universe", "core"):
            syms = payload.get("data")
            if not isinstance(syms, list):
                return (json.dumps({"error": "data[] required"}), 400, _JSON)
            # union with the stored list so a browser that lacks a UI-added
            # ticker can never delete it — the list only grows
            clean = merge_universe(_read(kind), syms)
            _write(kind, clean)
            return (json.dumps({"ok": True, "count": len(clean)}), 200, _JSON)

        if kind == "settings":
            cfg = payload.get("data")
            if not isinstance(cfg, dict):
                return (json.dumps({"error": "data{} required"}), 400, _JSON)
            clean = {}
            for k in ("gainPct", "highZonePct", "lowZonePct"):
                if k in cfg:
                    try:
                        clean[k] = float(cfg[k])
                    except (TypeError, ValueError):
                        pass
            if cfg.get("horizon") in ("long", "swing"):
                clean["horizon"] = cfg["horizon"]
            if cfg.get("reEntryMode") in ("base", "near_low"):
                clean["reEntryMode"] = cfg["reEntryMode"]
            # Settings > Alerts: which channels, and which alerts at all
            if cfg.get("alertChannel") in ("telegram", "email", "both"):
                clean["alertChannel"] = cfg["alertChannel"]
            types = cfg.get("alertTypes")
            if isinstance(types, dict):
                from alerts_email import ALERT_KEYS   # same --source dir
                known = {k: bool(types[k]) for k in ALERT_KEYS if k in types}
                if known:
                    clean["alertTypes"] = known
            # the written signals under the poster — absent means off, so this
            # only ever stores the switch when it is deliberately turned on
            if cfg.get("captionText") is True:
                clean["captionText"] = True
            # extra summary recipients. Validated and capped HERE as well as in
            # the browser: this endpoint is unauthenticated, so the stored list
            # has to be safe to hand straight to smtplib.
            if "alertEmails" in cfg:
                from alerts_email import clean_emails
                addrs = clean_emails(cfg.get("alertEmails"))
                if addrs:
                    clean["alertEmails"] = addrs
            w = cfg.get("weights")
            if isinstance(w, dict):
                try:
                    wv = {k: float(w[k]) for k in
                          ("trend", "momentum", "participation", "relStrength", "risk")}
                    if abs(sum(wv.values()) - 100) < 0.01:
                        clean["weights"] = wv
                except (KeyError, TypeError, ValueError):
                    pass
            _write("settings", clean)
            return (json.dumps({"ok": True, "saved": sorted(clean)}), 200, _JSON)

        if kind == "positions":
            posmap = payload.get("data")
            if not isinstance(posmap, dict):
                return (json.dumps({"error": "data{} required"}), 400, _JSON)
            clean = {}
            for sym, p in list(posmap.items())[:500]:
                if not isinstance(p, dict):
                    continue
                try:
                    entry = float(p.get("entry"))
                except (TypeError, ValueError):
                    continue
                clean[str(sym)[:16]] = {
                    "entry": round(entry, 4),
                    "date": str(p.get("date", ""))[:10],
                    "booked": bool(p.get("booked", False)),
                    "bookedDate": str(p.get("bookedDate", ""))[:10],
                }
            _write("positions", clean)
            return (json.dumps({"ok": True, "count": len(clean)}), 200, _JSON)

        if kind == "pbre":
            m = payload.get("data")
            if not isinstance(m, dict):
                return (json.dumps({"error": "data{} required"}), 400, _JSON)
            clean = {}
            for sym, st in list(m.items())[:500]:
                if not isinstance(st, dict):
                    continue
                e = {}
                if st.get("pb"):
                    e["pb"] = 1
                if st.get("re"):
                    e["re"] = 1
                if e:
                    clean[str(sym)[:16]] = e
            _write("pbre", clean)
            return (json.dumps({"ok": True, "count": len(clean)}), 200, _JSON)

        symbol = payload.get("symbol")
        notes_list = payload.get("notes")
        if not symbol or not isinstance(notes_list, list):
            return (json.dumps({"error": "symbol and notes[] required"}), 400, _JSON)
        clean = [
            {"text": str(n.get("text", ""))[:2000], "date": str(n.get("date", ""))[:32]}
            for n in notes_list[:200]
        ]
        all_notes = _read("notes")
        all_notes[symbol] = clean
        _write("notes", all_notes)
        return (json.dumps({"ok": True, "count": len(clean)}), 200, _JSON)

    return ("method not allowed", 405, _CORS)
