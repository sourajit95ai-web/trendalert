"""
chart_backend.py — pipeline additions for the charting dashboard.

Drop these into your existing main.py (or import them). They cover the three
backend changes the chart needs:
  1. fetch_crypto_bars()        — BTC/USD via Alpaca crypto (daily bars)
  2. support_resistance()       — server-side swing-pivot S/R levels
  3. build_chart_json()         — per-symbol OHLCV history -> chart.json on GCS

Notes persistence is a separate HTTP function (see notes_function.py), since the
browser writes to it directly.

No new heavy deps: requests + pandas (already in your stack). pandas-ta avoided.
"""

import os
import requests
import pandas as pd

ALPACA_KEY = os.environ.get("ALPACA_KEY_ID", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
CRYPTO_URL = "https://data.alpaca.markets/v1beta3/crypto/us/bars"


# ----------------------------------------------------------------------
# 1. Bitcoin via Alpaca crypto (daily bars, same shape as your equity bars)
# ----------------------------------------------------------------------
def fetch_crypto_bars(symbol="BTC/USD", days=400, timeframe="1Day"):
    """
    Returns a DataFrame with columns [open, high, low, close, volume],
    DatetimeIndex ascending — identical shape to your equity bars so it
    flows through the same RSI/MACD/EMA + scoring path unchanged.

    Crypto trades 24/7; daily bars are still daily, so indicators line up
    with equities. (Run it on the same 30-min scheduler — the latest daily
    bar just updates intraday.)
    """
    start = (pd.Timestamp.utcnow() - pd.Timedelta(days=int(days * 1.1))).date().isoformat()
    headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    params = {"symbols": symbol, "timeframe": timeframe, "start": start, "limit": 10000}

    rows, page_token = [], None
    while True:
        if page_token:
            params["page_token"] = page_token
        r = requests.get(CRYPTO_URL, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        rows.extend(data.get("bars", {}).get(symbol, []))
        page_token = data.get("next_page_token")
        if not page_token:
            break

    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["t"] = pd.to_datetime(df["t"])
    df = df.set_index("t").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


# ----------------------------------------------------------------------
# 2. Support / Resistance — swing-pivot detection + clustering
# ----------------------------------------------------------------------
def support_resistance(df, lookback=5, max_levels=4, tol_pct=0.012):
    """
    Detect pivot highs/lows (a bar that is the local extreme over +/- `lookback`
    bars), cluster nearby pivots, and return the strongest levels.

    Returns: [{"price": float, "kind": "support"|"resistance", "touches": int}]
    Sorted by strength (number of touches). `kind` is assigned relative to the
    latest close so the dashboard colors them correctly.
    """
    highs, lows, close = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    pivots = []
    for i in range(lookback, n - lookback):
        window_hi = highs[i - lookback:i + lookback + 1]
        window_lo = lows[i - lookback:i + lookback + 1]
        if highs[i] == window_hi.max():
            pivots.append(highs[i])
        if lows[i] == window_lo.min():
            pivots.append(lows[i])

    if not pivots:
        return []

    last = float(close[-1])
    tol = last * tol_pct
    clusters = []
    for p in sorted(pivots):
        hit = next((c for c in clusters if abs(c["price"] - p) < tol), None)
        if hit:
            hit["price"] = (hit["price"] * hit["touches"] + p) / (hit["touches"] + 1)
            hit["touches"] += 1
        else:
            clusters.append({"price": float(p), "touches": 1})

    for c in clusters:
        c["kind"] = "resistance" if c["price"] >= last else "support"
        c["price"] = round(c["price"], 2)

    clusters.sort(key=lambda c: c["touches"], reverse=True)
    return clusters[:max_levels]


# ----------------------------------------------------------------------
# 3. Build chart.json — the OHLCV history the dashboard charts read
# ----------------------------------------------------------------------
def build_chart_json(frames, tail=420):
    """
    frames: {symbol: OHLCV DataFrame (ascending DatetimeIndex)}
    Returns a dict the dashboard consumes directly:
        { "AAPL": [ {time, open, high, low, close, volume, sr:[...]}, ... ], ... }

    Only the last `tail` bars are emitted (keeps chart.json small; the chart's
    default span is 6 months ~= 126 trading days, 1Y view still covered).
    S/R is embedded on the LAST bar so the dashboard can render auto levels
    without recomputing — it still supports manual override client-side.
    """
    out = {}
    for sym, df in frames.items():
        d = df.tail(tail).copy()
        bars = [
            {
                "time": ts.strftime("%Y-%m-%d"),
                "open": round(float(o), 2), "high": round(float(h), 2),
                "low": round(float(l), 2), "close": round(float(c), 2),
                "volume": int(v),
            }
            for ts, o, h, l, c, v in zip(
                d.index, d["open"], d["high"], d["low"], d["close"], d["volume"]
            )
        ]
        if bars:
            bars[-1]["sr"] = support_resistance(df)   # levels computed on full history
        out[sym] = bars
    return out


# ----------------------------------------------------------------------
# 4. Enrich data.json summary records — 52-week fields + score inputs
# ----------------------------------------------------------------------
ZONE_HIGH_PCT = 2.0    # within 2% of 52w high  -> exit-review zone
ZONE_LOW_PCT = 10.0    # within 10% above 52w low -> re-entry watch zone


def _atr_last(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / n, adjust=False).mean().iloc[-1])


FULL_YEAR_BARS = 252   # trading days required before 52w levels / zones / base calls

def enrich_summary_records(frames, scored=None):
    """
    Per-symbol extra fields to merge into each data.json record.

    Adds the 52-week strategy fields (high_252, low_252, pct_from_high,
    pct_from_low, zone) AND the previously-missing score inputs
    (rel_volume, atr, rs_score, score, components) so the dashboard's
    clientScore approximation is replaced by real values.

    Usage in main.py, after fetching frames and running compute_scores:
        scored = compute_scores(frames, frames["SPY"])
        extra = enrich_summary_records(frames, scored)
        for sym, rec in data_records.items():
            rec.update(extra.get(sym, {}))
    """
    out = {}
    for sym, df in frames.items():
        if len(df) < 30:
            continue
        n = len(df)
        # A "52-week" level needs a 52-week window. New listings / recent IPOs have
        # fewer bars, and df.tail(252) would silently return a 3-month high labeled
        # as a 52-week high — arming the booking rule against a fake level.
        # Below FULL_YEAR_BARS we publish NO 52w fields and NO zone: the strategy
        # rules stay disarmed until the symbol has earned a real one-year range.
        full_year = n >= FULL_YEAR_BARS
        tail = df.tail(252)
        close = float(df["close"].iloc[-1])
        if full_year:
            hi = float(tail["high"].max())
            lo = float(tail["low"].min())
            pct_hi = (close - hi) / hi * 100 if hi else None
            pct_lo = (close - lo) / lo * 100 if lo else None
            zone = None
            if pct_hi is not None and pct_hi >= -ZONE_HIGH_PCT:
                zone = "near_high"
            elif pct_lo is not None and pct_lo <= ZONE_LOW_PCT:
                zone = "near_low"
        else:
            hi = lo = pct_hi = pct_lo = zone = None

        vol_ma = df["volume"].rolling(50).mean().iloc[-1]
        rvol = float(df["volume"].iloc[-1] / vol_ma) if vol_ma else None

        rec = {
            "history_bars": n,
            "limited_history": not full_year,
            "high_252": round(hi, 2) if hi is not None else None,
            "low_252": round(lo, 2) if lo is not None else None,
            "pct_from_high": round(pct_hi, 2) if pct_hi is not None else None,
            "pct_from_low": round(pct_lo, 2) if pct_lo is not None else None,
            "zone": zone,
            "rel_volume": round(rvol, 2) if rvol is not None else None,
            "atr": round(_atr_last(df), 2),
        }
        rec.update(base_formation(df))   # base_status / base_score / days_since_low
        if scored and sym in scored and scored[sym].get("score") is not None:
            rec["score"] = scored[sym]["score"]
            rec["components"] = scored[sym]["components"]
            rec["rs_score"] = scored[sym]["components"].get("rel_strength")
        out[sym] = rec
    return out


# ----------------------------------------------------------------------
# 5. Base-formation re-entry detector
# ----------------------------------------------------------------------
def base_formation(df, low_zone_pct=25.0, min_base_days=15):
    """
    Detects whether a symbol near its 52-week low has formed a credible base —
    the safer implementation of "re-enter near the low."

    A base requires ALL of:
      1. Proximity  : price within `low_zone_pct` above the 52w low
      2. Time       : >= `min_base_days` trading days since the 52w low was set
                      (rules out catching the knife on the way down)
      3. Stability  : 20-day realized volatility below its own 60-day average
                      (contraction = sellers exhausted)
      4. Reclaim    : close above EMA50 (first objective sign of accumulation)
      5. Volume     : 20-day avg volume not expanding to the downside —
                      down-day volume <= up-day volume over the last 20 bars

    Returns dict:
      {"base_status": "confirmed"|"forming"|"none",
       "base_score": 0-5 (how many conditions pass),
       "days_since_low": int}
    "forming" = proximity + time pass but not all of stability/reclaim/volume.
    """
    if len(df) < FULL_YEAR_BARS:
        # no credible 52-week low yet -> no base call (new listing)
        return {"base_status": "insufficient_history" if len(df) < FULL_YEAR_BARS else "none",
                "base_score": 0, "days_since_low": None}

    tail = df.tail(252)
    close = float(df["close"].iloc[-1])
    lo = float(tail["low"].min())
    lo_idx = tail["low"].idxmin()
    days_since_low = int((tail.index > lo_idx).sum())

    # 1. proximity
    near_low = lo > 0 and (close - lo) / lo * 100 <= low_zone_pct
    # 2. time since low
    aged = days_since_low >= min_base_days
    # 3. volatility stability: contracting OR already low vs the year's norm
    rets = df["close"].pct_change()
    vol20 = rets.rolling(20).std().iloc[-1]
    vol60 = rets.rolling(60).std().iloc[-1]
    vol_med = rets.rolling(20).std().tail(252).median()
    contracting = False
    if vol20 == vol20:  # not NaN
        contracting = bool((vol60 == vol60 and vol20 < vol60) or
                           (vol_med == vol_med and vol20 <= vol_med))
    # 4. EMA50 reclaim
    ema50 = df["close"].ewm(span=50, adjust=False).mean().iloc[-1]
    reclaimed = close > float(ema50)
    # 5. volume balance (accumulation vs distribution)
    last20 = df.tail(20)
    up_vol = last20.loc[last20["close"] >= last20["close"].shift(), "volume"].sum()
    dn_vol = last20.loc[last20["close"] < last20["close"].shift(), "volume"].sum()
    vol_ok = bool(up_vol >= dn_vol)

    checks = [near_low, aged, contracting, reclaimed, vol_ok]
    score = int(sum(checks))
    if near_low and aged and score == 5:
        status = "confirmed"
    elif near_low and aged:
        status = "forming"
    else:
        status = "none"
    return {"base_status": status, "base_score": score, "days_since_low": days_since_low}



# frames = fetch_equity_bars(SYMBOLS)          # your existing Alpaca multi-symbol call
# frames["BTC/USD"] = fetch_crypto_bars()      # add Bitcoin
# scored = compute_scores(frames, frames["SPY"])          # scoring.py
# extra = enrich_summary_records(frames, scored)          # 52w + score inputs
# for sym, rec in data_records.items(): rec.update(extra.get(sym, {}))
# chart_json = build_chart_json(frames)
# publish_to_gcs(chart_json, object_name="chart.json")   # alongside data.json


# ----------------------------------------------------------------------
# 6. settings.json bridge — dashboard settings drive the backend score
# ----------------------------------------------------------------------
def load_published_settings(bucket_name, object_name="settings.json"):
    """
    Reads gs://<bucket>/settings.json (published by the notes function when the
    dashboard saves settings with kind=settings). Returns a dict or {}.

    Wire into main.py BEFORE scoring:
        cfg = load_published_settings(BUCKET)
        if cfg.get("weights"):
            w = cfg["weights"]  # percentages summing to 100
            import scoring
            scoring.WEIGHTS = {
                "trend": w["trend"] / 100, "momentum": w["momentum"] / 100,
                "participation": w["participation"] / 100,
                "rel_strength": w["relStrength"] / 100, "risk_adj": w["risk"] / 100,
            }
        if cfg.get("horizon") == "swing":
            import scoring
            scoring.RS_WEIGHTS = {63: 0.40, 126: 0.30, 189: 0.20, 252: 0.10}

    Fails safe: any error returns {} and the pipeline runs on defaults.
    """
    try:
        from google.cloud import storage as gcs
        blob = gcs.Client().bucket(bucket_name).blob(object_name)
        if not blob.exists():
            return {}
        import json as _json
        cfg = _json.loads(blob.download_as_text() or "{}")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}
