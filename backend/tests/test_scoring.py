"""Regression tests: scoring buckets + base-formation across market regimes."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
from scoring import compute_scores
from chart_backend import base_formation, enrich_summary_records

rng = np.random.default_rng(7)

def synth(trend_bias, n=300):
    drift = trend_bias / 100
    rets = rng.normal(drift, 0.015, n)
    close = 100 * np.exp(np.cumsum(rets))
    return pd.DataFrame({"open": close, "high": close * (1 + np.abs(rng.normal(0, .006, n))),
                         "low": close * (1 - np.abs(rng.normal(0, .006, n))),
                         "close": close, "volume": rng.integers(8e5, 5e6, n).astype(float)})

def test_score_ordering():
    spy = synth(0.4)
    frames = {"STRONG": synth(1.2), "FLAT": synth(0.0), "WEAK": synth(-0.8)}
    res = compute_scores(frames, spy)
    assert res["STRONG"]["score"] > res["FLAT"]["score"] > res["WEAK"]["score"]

def test_enrich_zones():
    up = 100 * np.cumprod(1 + np.abs(rng.normal(0.001, 0.004, 300)))
    df = pd.DataFrame({"open": up, "high": up * 1.001, "low": up * 0.999,
                       "close": up, "volume": np.full(300, 2e6)})
    rec = enrich_summary_records({"X": df})["X"]
    assert rec["zone"] == "near_high" and rec["pct_from_high"] > -2

def test_base_knife_rejected():
    knife = 100 * np.cumprod(1 - np.abs(rng.normal(0.005, 0.01, 260)))
    df = pd.DataFrame({"open": knife, "high": knife * 1.004, "low": knife * 0.996,
                       "close": knife, "volume": np.full(260, 2e6)})
    r = base_formation(df)
    assert r["base_status"] == "none"
