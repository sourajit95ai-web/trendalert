"""
scoring.py — composite 0–100 signal score (long-term / 252-day horizon).

Composite:
    Score = 0.30*trend + 0.20*momentum + 0.20*participation
          + 0.20*rel_strength + 0.10*risk_adj

Each sub-score is 0–100. Relative strength is CROSS-SECTIONAL (percentile rank
across the whole universe), so it is computed in one pass over all symbols.

Inputs:
    frames: {symbol: OHLCV DataFrame} — columns ['open','high','low','close','volume'],
            ascending by date, >= 252 rows of history.
    spy:    SPY OHLCV DataFrame (same shape) — benchmark for relative strength.

Drop-in usage (inside your existing main.py, after you fetch the bars):
    from scoring import compute_scores
    scored = compute_scores(frames, spy)        # {sym: {'score':.., 'components':{..}}}
    for sym, rec in data_records.items():
        rec['score']      = scored[sym]['score']
        rec['components'] = scored[sym]['components']
    # then publish data.json as before
"""

import numpy as np
import pandas as pd


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def _true_range(df):
    h, l, c = df["high"], df["low"], df["close"]
    return pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)


def _atr(df, n=14):
    return _true_range(df).ewm(alpha=1 / n, adjust=False).mean()


def _adx(df, n=14):
    h, l = df["high"], df["low"]
    up, dn = h.diff(), -l.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    atr = _true_range(df).ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0)


# ----------------------------------------------------------------------
# sub-scores (latest value, 0..100)
# ----------------------------------------------------------------------
def trend_score(df):
    """EMA stacking (alignment) gated by ADX (trend strength, not chop)."""
    c = df["close"]
    px = c.iloc[-1]
    e20, e50, e200 = _ema(c, 20).iloc[-1], _ema(c, 50).iloc[-1], _ema(c, 200).iloc[-1]
    cond = [px > e20, e20 > e50, e50 > e200, px > e200]
    align = sum(cond) / len(cond)                       # 0..1
    adx = _adx(df).iloc[-1]
    strength = _clamp((adx - 15) / (35 - 15))           # ADX 15->0, 35->1
    return (0.70 * align + 0.30 * strength) * 100


def momentum_score(df):
    """MACD histogram (price-normalized) + RSI with an overbought taper."""
    c = df["close"]
    macd = _ema(c, 12) - _ema(c, 26)
    sig = _ema(macd, 9)
    hist_n = (macd - sig).iloc[-1] / c.iloc[-1]         # as % of price
    macd_comp = _clamp((hist_n + 0.01) / 0.02)          # -1%->0, +1%->1

    rsi = _rsi(c).iloc[-1]
    if rsi <= 70:
        rsi_comp = _clamp((rsi - 30) / (70 - 30))       # 30->0, 70->1
    else:
        rsi_comp = _clamp(1 - (rsi - 70) / 30)          # taper down past 70 (overbought)
    return (0.60 * macd_comp + 0.40 * rsi_comp) * 100


def participation_score(df, vol_n=50, obv_n=50):
    """Relative volume (magnitude) x OBV-vs-EMA (direction of flow)."""
    v = df["volume"]
    rvol = v.iloc[-1] / v.rolling(vol_n).mean().iloc[-1]
    mag = _clamp((rvol - 0.5) / (2.5 - 0.5)) * 100      # 0.5x->0, 2.5x->100

    sign = np.sign(df["close"].diff()).fillna(0)
    obv = (sign * v).cumsum()
    spread = obv.iloc[-1] - _ema(obv, obv_n).iloc[-1]
    denom = obv.diff().rolling(obv_n).std().iloc[-1] * np.sqrt(obv_n)
    dir_z = spread / denom if denom and not np.isnan(denom) else 0.0
    dir_comp = _clamp((dir_z + 2) / 4) * 100            # z -2->0, +2->100
    return 0.60 * mag + 0.40 * dir_comp


def risk_score(df, atr_n=14):
    """Lower volatility + not overextended above EMA200 = higher (safer) score."""
    c = df["close"]
    atrp = _atr(df, atr_n).iloc[-1] / c.iloc[-1]
    vol_comp = _clamp(1 - (atrp - 0.01) / (0.05 - 0.01))        # 1% ATR->1, 5%->0
    e200 = _ema(c, 200).iloc[-1]
    stretch = (c.iloc[-1] - e200) / e200
    stretch_comp = _clamp(1 - max(0.0, stretch - 0.10) / 0.40)  # >10% above starts penalizing
    return (0.50 * vol_comp + 0.50 * stretch_comp) * 100


# ----------------------------------------------------------------------
# relative strength (cross-sectional, 252-day weighted blend)
# ----------------------------------------------------------------------
# 252-day blend: full year weighted most, recent quarter least.
RS_WEIGHTS = {252: 0.40, 189: 0.30, 126: 0.20, 63: 0.10}


def _blended_excess(df, spy, weights=RS_WEIGHTS):
    c, s = df["close"], spy["close"]
    ex, used = 0.0, 0.0
    for days, w in weights.items():
        if len(c) > days and len(s) > days:
            r_stock = c.iloc[-1] / c.iloc[-1 - days] - 1
            r_spy = s.iloc[-1] / s.iloc[-1 - days] - 1
            ex += w * (r_stock - r_spy)
            used += w
    return ex / used if used else 0.0                   # re-normalize if a window was missing


def relative_strength_scores(frames, spy):
    """{symbol: 0..100 percentile rank of blended excess return vs SPY}."""
    excess = {sym: _blended_excess(df, spy) for sym, df in frames.items()}
    ser = pd.Series(excess)
    if len(ser) < 3:                                    # too few names to rank meaningfully
        return {sym: 50.0 for sym in frames}
    return (ser.rank(pct=True) * 100).to_dict()


# ----------------------------------------------------------------------
# composite
# ----------------------------------------------------------------------
WEIGHTS = {
    "trend": 0.30,
    "momentum": 0.20,
    "participation": 0.20,
    "rel_strength": 0.20,
    "risk_adj": 0.10,
}


def compute_scores(frames, spy):
    """
    frames: {symbol: OHLCV DataFrame (>=252 rows, ascending by date)}
    spy:    SPY OHLCV DataFrame
    returns: {symbol: {'score': float|None, 'components': {...}}}
    """
    rs = relative_strength_scores(frames, spy)
    out = {}
    for sym, df in frames.items():
        if len(df) < 252:
            out[sym] = {"score": None, "components": {}, "note": "need >=252 rows"}
            continue
        comp = {
            "trend": round(trend_score(df), 1),
            "momentum": round(momentum_score(df), 1),
            "participation": round(participation_score(df), 1),
            "rel_strength": round(rs.get(sym, 50.0), 1),
            "risk_adj": round(risk_score(df), 1),
        }
        score = sum(WEIGHTS[k] * comp[k] for k in WEIGHTS)
        out[sym] = {"score": round(score, 1), "components": comp}
    return out


# ----------------------------------------------------------------------
# standalone demo (synthetic data) — run: python scoring.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    rng = np.random.default_rng(7)

    def synth(trend_bias, n=300):
        drift = trend_bias / 100
        rets = rng.normal(drift, 0.015, n)
        close = 100 * np.exp(np.cumsum(rets))
        high = close * (1 + np.abs(rng.normal(0, 0.006, n)))
        low = close * (1 - np.abs(rng.normal(0, 0.006, n)))
        vol = rng.integers(8e5, 5e6, n).astype(float)
        return pd.DataFrame({"open": close, "high": high, "low": low,
                             "close": close, "volume": vol})

    spy = synth(0.4)
    frames = {"STRONG": synth(1.2), "FLAT": synth(0.0), "WEAK": synth(-0.8)}
    for sym, r in compute_scores(frames, spy).items():
        print(f"{sym:7} score={r['score']}  {r['components']}")
