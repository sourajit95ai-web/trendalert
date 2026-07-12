"""End-to-end pipeline test — guards the publish path.

Runs synthetic frames through the full chain:
    compute_scores -> enrich_summary_records -> build_chart_json
then schema-validates both outputs. This is the test that protects your live
data.json/chart.json from being overwritten by structurally broken payloads.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd

from scoring import compute_scores
from chart_backend import enrich_summary_records, build_chart_json

rng = np.random.default_rng(99)


def synth(trend_bias, n=300, base=100):
    drift = trend_bias / 100
    rets = rng.normal(drift, 0.012, n)
    close = base * np.exp(np.cumsum(rets))
    # rollback: a weekend 'today' otherwise yields periods-1 rows (weekend-flaky CI)
    idx = pd.bdate_range(end=pd.offsets.BDay().rollback(pd.Timestamp.today()), periods=n)
    return pd.DataFrame({
        "open": close, "high": close * (1 + np.abs(rng.normal(0, .005, n))),
        "low": close * (1 - np.abs(rng.normal(0, .005, n))),
        "close": close, "volume": rng.integers(8e5, 5e6, n).astype(float),
    }, index=idx)


UNIVERSE = {"AAPL": 0.10, "MSFT": 0.06, "NVDA": 0.14, "TSLA": -0.05,
            "XOM": 0.02, "JPM": 0.04, "CAT": 0.00, "UNH": -0.02,
            "SPY": 0.05, "BTC/USD": 0.12}

SUMMARY_REQUIRED = {"high_252", "low_252", "pct_from_high", "pct_from_low",
                    "zone", "rel_volume", "atr", "base_status", "base_score",
                    "days_since_low", "score", "components", "rs_score"}
COMPONENT_KEYS = {"trend", "momentum", "participation", "rel_strength", "risk_adj"}
BAR_REQUIRED = {"time", "open", "high", "low", "close", "volume"}


def _run_pipeline():
    frames = {sym: synth(b) for sym, b in UNIVERSE.items()}
    scored = compute_scores(frames, frames["SPY"])
    extra = enrich_summary_records(frames, scored)
    chart = build_chart_json(frames)
    return frames, scored, extra, chart


def test_pipeline_summary_schema():
    frames, scored, extra, _ = _run_pipeline()
    for sym in UNIVERSE:
        rec = extra[sym]
        missing = SUMMARY_REQUIRED - set(rec)
        assert not missing, f"{sym} missing fields: {missing}"
        assert COMPONENT_KEYS == set(rec["components"]), f"{sym} components malformed"
        assert 0 <= rec["score"] <= 100
        assert rec["zone"] in ("near_high", "near_low", None)
        assert rec["base_status"] in ("confirmed", "forming", "none")
        assert 0 <= rec["base_score"] <= 5
        assert rec["low_252"] <= rec["high_252"]


def test_pipeline_chart_schema():
    _, _, _, chart = _run_pipeline()
    for sym, bars in chart.items():
        assert len(bars) > 0, f"{sym} emitted no bars"
        assert len(bars) <= 420
        for bar in (bars[0], bars[-1]):
            assert BAR_REQUIRED <= set(bar), f"{sym} bar missing keys"
            assert bar["low"] <= bar["high"]
        # S/R embedded on last bar only, with valid shape
        assert "sr" in bars[-1]
        for lv in bars[-1]["sr"]:
            assert lv["kind"] in ("support", "resistance") and lv["price"] > 0
        # dates ascending
        assert bars[0]["time"] < bars[-1]["time"]


def test_pipeline_json_serializable():
    """The exact publish payloads must serialize — catches numpy leakage."""
    _, _, extra, chart = _run_pipeline()
    json.dumps(extra)
    json.dumps(chart)


def test_pipeline_rs_is_cross_sectional():
    _, scored, _, _ = _run_pipeline()
    rs_vals = [s["components"]["rel_strength"] for s in scored.values()
               if s["score"] is not None]
    # percentile ranks must actually spread across the universe
    assert max(rs_vals) > 80 and min(rs_vals) < 20
