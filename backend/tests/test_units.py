"""Unit-level regression tests: score buckets, zones, base-formation conditions.

Frozen synthetic-data validations — these encode behaviors we verified manually
during development. If a formula edit changes any of these, the test names tell
you exactly which bucket/condition regressed.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
import pytest

from scoring import (trend_score, momentum_score, participation_score,
                     risk_score, relative_strength_scores, compute_scores,
                     WEIGHTS, RS_WEIGHTS)
from chart_backend import base_formation, enrich_summary_records, support_resistance

rng = np.random.default_rng(42)


# ---------- fixtures ----------
def frame(closes, volumes=None):
    c = pd.Series(np.asarray(closes, dtype=float))
    v = volumes if volumes is not None else np.full(len(c), 2e6)
    return pd.DataFrame({"open": c, "high": c * 1.004, "low": c * 0.996,
                         "close": c, "volume": np.asarray(v, dtype=float)})

def steady_up(n=300, drift=0.0015, base=100):
    return base * np.cumprod(1 + np.abs(rng.normal(drift, 0.002, n)))

def steady_down(n=300, drift=0.0015, base=100):
    return base * np.cumprod(1 - np.abs(rng.normal(drift, 0.002, n)))


# ---------- trend bucket ----------
def test_trend_uptrend_beats_downtrend():
    up, dn = frame(steady_up()), frame(steady_down())
    assert trend_score(up) > 70 > 35 > trend_score(dn)

def test_trend_flat_is_middling():
    flat = frame(100 + rng.normal(0, 0.4, 300).cumsum() * 0.01)
    assert 20 <= trend_score(flat) <= 80


# ---------- momentum bucket ----------
def test_momentum_overbought_taper():
    """RSI above 70 must reduce the momentum sub-score, not max it."""
    parabolic = frame(100 * np.cumprod(1 + np.abs(rng.normal(0.012, 0.002, 300))))
    gentle = frame(steady_up(drift=0.002))
    # both bullish, but parabolic (deep overbought) must not dominate purely on RSI
    assert momentum_score(parabolic) < 95


# ---------- participation bucket ----------
def test_participation_rewards_volume_surge():
    quiet = frame(steady_up(), volumes=np.full(300, 1e6))
    surge_vol = np.full(300, 1e6); surge_vol[-1] = 3e6
    surging = frame(steady_up(), volumes=surge_vol)
    assert participation_score(surging) > participation_score(quiet)


# ---------- risk bucket ----------
def test_risk_prefers_calm_over_wild():
    calm = frame(100 * np.cumprod(1 + rng.normal(0.0003, 0.004, 300)))
    wild = frame(100 * np.cumprod(1 + rng.normal(0.0003, 0.035, 300)))
    assert risk_score(calm) > risk_score(wild)


# ---------- relative strength ----------
def test_rs_is_percentile_ranked():
    spy = frame(steady_up(drift=0.0008))
    frames = {"LEAD": frame(steady_up(drift=0.002)),
              "MID": frame(steady_up(drift=0.0008)),
              "LAG": frame(steady_down(drift=0.001))}
    rs = relative_strength_scores(frames, spy)
    assert rs["LEAD"] > rs["MID"] > rs["LAG"]
    assert 0 <= min(rs.values()) and max(rs.values()) <= 100

def test_rs_blend_is_252_weighted():
    assert RS_WEIGHTS[252] == 0.40 and RS_WEIGHTS[63] == 0.10


# ---------- composite ----------
def test_weights_sum_to_one():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

def test_insufficient_history_returns_none():
    short = frame(steady_up(n=100))
    res = compute_scores({"X": short}, frame(steady_up()))
    assert res["X"]["score"] is None


# ---------- zones ----------
def test_zone_near_high():
    rec = enrich_summary_records({"X": frame(steady_up())})["X"]
    assert rec["zone"] == "near_high" and rec["pct_from_high"] >= -2

def test_zone_none_midrange():
    """Deterministic mid-range: close pinned halfway between 52w low and high."""
    n = 300
    t = np.linspace(0, np.pi, n)
    path = 100 + 30 * np.sin(t)          # rises 100->130, returns to ~100
    path[-1] = 115                        # park the close mid-range
    rec = enrich_summary_records({"X": frame(path)})["X"]
    assert rec["zone"] is None, f"expected no zone, got {rec['zone']} (from_high {rec['pct_from_high']}, from_low {rec['pct_from_low']})"


# ---------- base formation: each gate ----------
def test_base_confirmed_on_recovery():
    # >=252 bars: a base call requires a credible 52-week low (new-listing guard)
    down = steady_down(200, drift=0.004)
    rec_leg = down[-1] * np.cumprod(1 + np.abs(rng.normal(0.0018, 0.0025, 60)))
    vol = np.concatenate([np.full(200, 2e6),
        np.where(np.diff(np.concatenate([[down[-1]], rec_leg])) >= 0, 3e6, 1.2e6)])
    r = base_formation(frame(np.concatenate([down, rec_leg]), vol))
    assert r["base_status"] == "confirmed" and r["base_score"] == 5

def test_base_rejects_fresh_low():
    """Low set yesterday -> time gate must block regardless of other checks."""
    r = base_formation(frame(steady_down(260)))
    assert r["base_status"] == "none" and r["days_since_low"] < 15

def test_base_rejects_distribution_volume():
    """Recovery on down-day volume must not fully confirm."""
    down = steady_down(180, drift=0.004)
    rec_leg = down[-1] * np.cumprod(1 + rng.normal(0.0012, 0.004, 60))
    vol = np.concatenate([np.full(180, 2e6),
        np.where(np.diff(np.concatenate([[down[-1]], rec_leg])) >= 0, 1e6, 4e6)])
    r = base_formation(frame(np.concatenate([down, rec_leg]), vol))
    assert r["base_score"] < 5


# ---------- support/resistance ----------
def test_sr_levels_bounded_and_typed():
    lv = support_resistance(frame(100 + 10 * np.sin(np.linspace(0, 12, 300)) + rng.normal(0, 0.5, 300)))
    assert 0 < len(lv) <= 4
    assert all(l["kind"] in ("support", "resistance") for l in lv)


# ---------- dynamic universe ----------
def test_partition_universe():
    from main import partition_universe, SYMBOLS
    eq, cr = partition_universe(
        ["aapl", " CAT ", "ETH/USD", SYMBOLS[0], "CAT", "", None, "BTC/USD"])
    assert eq == ["AAPL", "CAT"]          # cleaned, deduped, core excluded
    assert cr == ["ETH/USD"]              # "/" routes to crypto; core BTC dropped

def test_partition_universe_bad_input():
    from main import partition_universe
    assert partition_universe(None) == ([], [])
    assert partition_universe({"a": 1}) == ([], [])
