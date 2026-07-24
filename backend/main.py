"""
main.py — TrendAlert data pipeline (Cloud Function, entry point: main).

Flow (every 30 min via Cloud Scheduler -> HTTP trigger):
  1. Fetch ~420 daily bars for all SYMBOLS (Alpaca stocks) + BTC/USD (Alpaca crypto)
  2. Apply published settings.json if present (weights / horizon -> scoring)
  3. Compute composite scores (scoring.py) + indicators per symbol
  4. Detect EMA50/150 cross alerts (golden / death cross)
  5. Enrich with 52w fields, zones, base formation (chart_backend.py)
  6. Publish data.json + chart.json to GCS — guarded: aborts without
     overwriting if the fetch or schema looks broken

Env (deploy.yml sets these):
  ALPACA_KEY_ID / ALPACA_SECRET_KEY  (from Secret Manager via --set-secrets)
  GCS_BUCKET
"""

import json
import os
from datetime import datetime, timezone

import pandas as pd
import requests
from google.cloud import storage

import scoring
from trading_calendar import is_trading_day, last_completed_trading_day, _eastern_now
from alerts_email import send_eod_alerts
from scoring import compute_scores
from chart_backend import (
    fetch_crypto_bars,
    enrich_summary_records,
    build_chart_json,
    load_published_settings,
)

# ----------------------------------------------------------------------
# config — EDIT SYMBOLS/SECTORS to your universe
# ----------------------------------------------------------------------
SYMBOLS = [
    "META", "MELI", "MSFT", "GOOGL", "AMZN", "MA", "NFLX", "NVDA", "AMD", "NOW",
    "V", "UNH", "COIN", "VEEV", "HOOD", "TSM", "CRWD", "PANW", "AXON", "LLY",
    "TMO", "NBIS", "AVGO", "ISRG", "PLTR", "RKLB", "IONQ", "NVO",
    "SPY", "QQQ", "SOXX", "IWM",   # SPY is the RS benchmark; ETFs feed the Index list
    "IAUM", "IJR", "SPGI", "IHAK", "IGV", "VOO",
]
CRYPTO_SYMBOLS = ["BTC/USD"]
SECTORS = {
    "META": "Technology", "MELI": "Consumer", "MSFT": "Technology",
    "GOOGL": "Technology", "AMZN": "Consumer", "MA": "Finance",
    "NFLX": "Consumer", "NVDA": "Technology", "AMD": "Technology",
    "NOW": "Technology", "V": "Finance", "UNH": "Healthcare",
    "COIN": "Finance", "VEEV": "Healthcare", "HOOD": "Finance",
    "TSM": "Technology", "CRWD": "Technology", "PANW": "Technology",
    "AXON": "Industrial", "LLY": "Healthcare", "TMO": "Healthcare",
    "NBIS": "Technology", "AVGO": "Technology", "ISRG": "Healthcare",
    "PLTR": "Technology", "RKLB": "Industrial", "IONQ": "Technology",
    "NVO": "Healthcare",
    "CRSP": "Healthcare", "NU": "Finance",   # UI-added extras — label so they don't read "Other"
    "SPY": "Index", "QQQ": "Index", "SOXX": "Index", "IWM": "Index",
    "IAUM": "Index", "IJR": "Index", "SPGI": "Finance", "IHAK": "Index",
    "IGV": "Index", "VOO": "Index",
    "BTC/USD": "Crypto",
}

MAX_EXTRA_EQUITY = 50       # dynamic-universe caps — one batched request stays one request
MAX_EXTRA_CRYPTO = 10
HISTORY_DAYS = 420          # calendar span requested (>=252 trading days needed)
TIMEFRAME = "1Day"
MIN_FETCH_RATIO = 0.9       # publish guard: abort if <90% of symbols fetched
BUCKET = os.environ.get("GCS_BUCKET", "")

ALPACA_KEY = os.environ.get("ALPACA_KEY_ID", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
STOCKS_URL = "https://data.alpaca.markets/v2/stocks/bars"
_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}


# ----------------------------------------------------------------------
# fetch
# ----------------------------------------------------------------------
def fetch_equity_bars(symbols, days=HISTORY_DAYS):
    """Multi-symbol daily bars -> {symbol: OHLCV DataFrame, ascending index}."""
    start = (pd.Timestamp.utcnow() - pd.Timedelta(days=int(days * 1.5))).date().isoformat()
    params = {
        "symbols": ",".join(symbols),
        "timeframe": TIMEFRAME,
        "start": start,
        "limit": 10000,
        "adjustment": "all",   # split AND dividend adjusted — indicator continuity
        "feed": "iex",              # free tier; switch to "sip" if subscribed
    }
    buckets = {s: [] for s in symbols}
    page_token = None
    while True:
        if page_token:
            params["page_token"] = page_token
        r = requests.get(STOCKS_URL, headers=_HEADERS, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for sym, rows in (data.get("bars") or {}).items():
            buckets.setdefault(sym, []).extend(rows)
        page_token = data.get("next_page_token")
        if not page_token:
            break

    frames = {}
    for sym, rows in buckets.items():
        if not rows:
            continue
        df = pd.DataFrame(rows).rename(
            columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        df["t"] = pd.to_datetime(df["t"])
        df = df.set_index("t").sort_index()
        frames[sym] = df[["open", "high", "low", "close", "volume"]]
    return frames


# ----------------------------------------------------------------------
# indicators (last-value fields the dashboard table reads)
# ----------------------------------------------------------------------
def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(50)


def indicator_record(sym, df):
    c = df["close"]
    close = float(c.iloc[-1])
    prev = float(c.iloc[-2]) if len(c) > 1 else close
    macd_line = _ema(c, 12) - _ema(c, 26)
    macd_sig = _ema(macd_line, 9)
    return {
        "symbol": sym,
        "sector": SECTORS.get(sym, "Other"),
        "as_of_date": df.index[-1].strftime("%Y-%m-%d"),
        "close": round(close, 2),
        "change": round(close - prev, 2),
        "change_pct": round((close - prev) / prev * 100, 2) if prev else 0.0,
        "rsi14": round(float(_rsi(c).iloc[-1]), 2),
        "macd": round(float(macd_line.iloc[-1]), 3),
        "macd_signal": round(float(macd_sig.iloc[-1]), 3),
        "macd_hist": round(float(macd_line.iloc[-1] - macd_sig.iloc[-1]), 3),
        # an EMA(n) is meaningless before n bars exist — publish null rather than
        # a number seeded from too little data (new listings / recent IPOs)
        "ema20": _ema_or_none(c, 20),
        "ema50": _ema_or_none(c, 50),
        "ema150": _ema_or_none(c, 150),
        "ema200": _ema_or_none(c, 200),
    }


def _ema_or_none(c, period):
    if len(c) < period:
        return None
    return round(float(_ema(c, period).iloc[-1]), 2)


def detect_cross_alerts(frames):
    """EMA50/EMA150 crosses on the latest bar -> dashboard alert cards."""
    alerts = []
    for sym, df in frames.items():
        if len(df) < 160:
            continue
        e50, e150 = _ema(df["close"], 50), _ema(df["close"], 150)
        now_above = e50.iloc[-1] > e150.iloc[-1]
        was_above = e50.iloc[-2] > e150.iloc[-2]
        if now_above and not was_above:
            alerts.append({"symbol": sym, "dir": "bull",
                           "type": "EMA 50 crossed above EMA 150", "detail": "Golden cross"})
        elif was_above and not now_above:
            alerts.append({"symbol": sym, "dir": "bear",
                           "type": "EMA 150 crossed below EMA 50", "detail": "Death cross"})
    return alerts


# ----------------------------------------------------------------------
# dynamic universe (universe.json — tickers added in the dashboard UI)
# ----------------------------------------------------------------------
def partition_universe(raw):
    """Sanitize a raw symbol list -> (extra_equities, extra_cryptos).

    Symbols already in the core universe are dropped; "/" marks a crypto
    pair (Alpaca style, e.g. ETH/USD). Pure — testable without GCS."""
    if not isinstance(raw, list):
        return [], []
    eq, cr, seen = [], [], set()
    for s in raw:
        if not isinstance(s, str):
            continue
        s = s.strip().upper()[:16]
        if not s or s in seen or s in SYMBOLS or s in CRYPTO_SYMBOLS:
            continue
        seen.add(s)
        (cr if "/" in s else eq).append(s)
    return eq[:MAX_EXTRA_EQUITY], cr[:MAX_EXTRA_CRYPTO]


def load_dynamic_universe():
    """universe.json in the bucket (published by the notes function)."""
    try:
        blob = storage.Client().bucket(BUCKET).blob("universe.json")
        if not blob.exists():
            return [], []
        return partition_universe(json.loads(blob.download_as_text()))
    except Exception:
        return [], []           # a broken universe file must never stop the run


# ----------------------------------------------------------------------
# settings bridge
# ----------------------------------------------------------------------
def apply_published_settings():
    cfg = load_published_settings(BUCKET)
    if not cfg:
        return "defaults"
    applied = []
    w = cfg.get("weights")
    if isinstance(w, dict) and abs(sum(w.values()) - 100) < 0.01:
        scoring.WEIGHTS = {
            "trend": w["trend"] / 100, "momentum": w["momentum"] / 100,
            "participation": w["participation"] / 100,
            "rel_strength": w["relStrength"] / 100, "risk_adj": w["risk"] / 100,
        }
        applied.append("weights")
    if cfg.get("horizon") == "swing":
        scoring.RS_WEIGHTS = {63: 0.40, 126: 0.30, 189: 0.20, 252: 0.10}
        applied.append("swing-RS")
    return "+".join(applied) or "defaults"


# ----------------------------------------------------------------------
# publish (guarded)
# ----------------------------------------------------------------------
def _publish(obj, name):
    blob = storage.Client().bucket(BUCKET).blob(name)
    blob.cache_control = "no-cache, max-age=0"
    blob.upload_from_string(json.dumps(obj, ensure_ascii=False),
                            content_type="application/json")


def _validate(payload, chart):
    """Schema guard mirroring tests/test_pipeline.py — never overwrite with junk."""
    assert payload["symbols"], "no symbol records"
    for rec in payload["symbols"]:
        assert {"symbol", "close", "rsi14", "ema20", "ema50", "ema200"} <= set(rec)
    for sym, bars in chart.items():
        assert bars and bars[0]["time"] < bars[-1]["time"], f"{sym} chart malformed"
    json.dumps(payload)
    json.dumps(chart)


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------
def main(request):
    # Scheduler side-modes — none recompute scores or touch data.json:
    #   ?mode=brief      9:50 ET intraday morning brief
    #   ?mode=bloodbath  8:30 ET pre-open crash alarm (silent on normal days)
    #   ?mode=summary    pre-market + post-close Core daily summary as a chart
    mode = request.args.get("mode") if request is not None \
        and getattr(request, "args", None) else None
    if mode in ("brief", "bloodbath", "summary"):
        if mode == "brief":
            from morning_brief import run_morning_brief
            res = run_morning_brief(BUCKET, SYMBOLS, SECTORS, _HEADERS)
        elif mode == "bloodbath":
            from bloodbath import run_bloodbath_check
            res = run_bloodbath_check(BUCKET, SYMBOLS, SECTORS, _HEADERS)
        else:
            from daily_summary import run_daily_summary
            res = run_daily_summary(BUCKET)
        return (json.dumps(res), 200 if res.get("ok") else 500,
                {"Content-Type": "application/json"})

    et_today = _eastern_now().date()
    trading_today = is_trading_day(et_today)
    expected_close = last_completed_trading_day()   # most recent SETTLED close

    # the publish guard is measured against the CORE universe only — a typo'd
    # ticker added in the UI must never be able to block publishing
    expected = SYMBOLS + CRYPTO_SYMBOLS
    extra_eq, extra_cr = load_dynamic_universe()

    frames = fetch_equity_bars(SYMBOLS + extra_eq)
    for cs in CRYPTO_SYMBOLS + extra_cr:
        try:
            df = fetch_crypto_bars(cs, days=HISTORY_DAYS)
            if len(df):
                frames[cs] = df
        except Exception:
            pass                                   # crypto failure isn't fatal

    # publish guard 1: enough of the CORE universe fetched?
    core_fetched = sum(1 for s in expected if s in frames)
    ratio = core_fetched / len(expected)
    if ratio < MIN_FETCH_RATIO:
        return (json.dumps({"ok": False,
                            "error": f"fetched {core_fetched}/{len(expected)} core — aborting, JSON not overwritten"}),
                500, {"Content-Type": "application/json"})

    settings_applied = apply_published_settings()

    spy = frames.get("SPY")
    scored = compute_scores(frames, spy) if spy is not None else {}
    extra = enrich_summary_records(frames, scored)

    records = []
    for sym, df in frames.items():
        if len(df) < 60:
            continue
        rec = indicator_record(sym, df)
        rec.update(extra.get(sym, {}))
        records.append(rec)

    equity_asof = max((r["as_of_date"] for r in records
                       if r["symbol"] in SYMBOLS), default=None)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "expected_last_trading_day": expected_close.isoformat(),
        "data_current": equity_asof == expected_close.isoformat(),
        "timeframe": TIMEFRAME,
        "settings_applied": settings_applied,
        "symbols": records,
        "alerts": detect_cross_alerts(frames),
    }
    chart = build_chart_json(frames)

    # publish guard 2: schema must hold
    try:
        _validate(payload, chart)
    except AssertionError as e:
        return (json.dumps({"ok": False, "error": f"schema guard: {e}"}),
                500, {"Content-Type": "application/json"})

    _publish(payload, "data.json")
    _publish(chart, "chart.json")

    # EOD alert email — transition-deduped, never fatal, only when the shown
    # closes are the settled closes of an actual trading day
    email_status = send_eod_alerts(
        records, payload["alerts"], bucket=BUCKET,
        asof=expected_close.isoformat(),
        trading_day=trading_today and payload["data_current"])

    return (json.dumps({"ok": True, "symbols": len(records),
                        "alerts": len(payload["alerts"]),
                        "email": email_status,
                        "data_current": payload["data_current"],
                        "settings": settings_applied}),
            200, {"Content-Type": "application/json"})

# entry-point re-export so Functions Framework can find the notes handler
from notes_function import notes  # noqa: F401
