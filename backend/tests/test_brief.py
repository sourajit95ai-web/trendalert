"""Morning-brief tests — record shaping only, no network/GCS/matplotlib.

The brief no longer composes any text or draws anything of its own: it turns
intraday snapshots into the record shape daily_summary.compute_summary
already consumes, and the close alert's renderer does the rest. So what
needs pinning here is the handoff — that the shaped records carry every
field the poster reads, that change_pct is measured against yesterday's
settled close, and that the live price (not data.json's stale one) is what
lands on the 52-week gauge.

The old tests asserted three text sections including "YOUR STOCKS", which
read a positions.json that is no longer written. That section is gone.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morning_brief import brief_records, _pct
from daily_summary import compute_summary


SNAPS = {
    # prev 100 -> opened 102 (gap +2%) -> now 101 (drift -0.98%, day +1%)
    "AAA": {"last": 101.0, "open": 102.0, "prev_close": 100.0, "bar_date": "2026-07-17"},
    "BBB": {"last": 95.0, "open": 99.0, "prev_close": 100.0, "bar_date": "2026-07-17"},
    "CCC": {"last": 103.0, "open": 100.5, "prev_close": 100.0, "bar_date": "2026-07-17"},
    "DDD": {"last": 97.0, "open": 99.0, "prev_close": 100.0, "bar_date": "2026-07-17"},
    "SPY": {"last": 500.0, "open": 498.0, "prev_close": 495.0, "bar_date": "2026-07-17"},
    "QQQ": {"last": 440.0, "open": 438.0, "prev_close": 435.0, "bar_date": "2026-07-17"},
}
# data.json carries sector + the 52-week bounds; close here is YESTERDAY's
DATA = [
    {"symbol": "AAA", "sector": "Technology", "close": 100.0, "low_252": 60.0, "high_252": 120.0},
    {"symbol": "BBB", "sector": "Technology", "close": 100.0, "low_252": 90.0, "high_252": 180.0},
    {"symbol": "CCC", "sector": "Finance", "close": 100.0, "low_252": 55.0, "high_252": 104.0},
    {"symbol": "DDD", "sector": "Finance", "close": 100.0, "low_252": 70.0, "high_252": 150.0},
    {"symbol": "SPY", "sector": "Index", "close": 495.0, "low_252": 400.0, "high_252": 510.0},
    {"symbol": "QQQ", "sector": "Index", "close": 435.0, "low_252": 350.0, "high_252": 450.0},
]
CORE = ["AAA", "BBB", "CCC", "DDD"]


def test_pct():
    assert _pct(102, 100) == 2.0
    assert _pct(100, 0) == 0.0


def test_change_pct_is_measured_against_yesterdays_close():
    """The same quantity the close alert plots, taken at a different hour."""
    recs = {r["symbol"]: r for r in brief_records(SNAPS, DATA)}
    assert round(recs["AAA"]["change_pct"], 4) == 1.0      # 100 -> 101
    assert round(recs["BBB"]["change_pct"], 4) == -5.0     # 100 -> 95
    assert round(recs["SPY"]["change_pct"], 4) == round(5 / 495 * 100, 4)


def test_close_is_the_live_price_not_the_settled_one():
    """The range gauge must mark where the name trades now."""
    recs = {r["symbol"]: r for r in brief_records(SNAPS, DATA)}
    assert recs["AAA"]["close"] == 101.0                   # snapshot, not DATA's 100
    assert recs["AAA"]["low_252"] == 60.0                  # bounds still from DATA
    assert recs["AAA"]["high_252"] == 120.0


def test_sector_carried_over_from_data_json():
    recs = {r["symbol"]: r for r in brief_records(SNAPS, DATA)}
    assert recs["CCC"]["sector"] == "Finance"
    assert recs["SPY"]["sector"] == "Index"


def test_symbol_missing_from_data_json_degrades_not_crashes():
    snaps = dict(SNAPS)
    snaps["ZZZ"] = {"last": 10.0, "open": 9.0, "prev_close": 9.5, "bar_date": "2026-07-17"}
    recs = {r["symbol"]: r for r in brief_records(snaps, DATA)}
    assert recs["ZZZ"]["sector"] == "Other"
    assert recs["ZZZ"]["low_252"] is None                  # no gauge, no crash


def test_records_feed_compute_summary_unchanged():
    """The whole point: the close alert's compute path accepts these as-is."""
    s = compute_summary(brief_records(SNAPS, DATA), CORE, "2026-07-17",
                        session="MORNING SESSION")
    assert s is not None
    assert s["session"] == "MORNING SESSION"
    assert s["up"][0][0] == "CCC"                          # +3%, best mover
    assert s["down"][0][0] == "BBB"                        # -5%, worst
    assert s["up_n"] == 2 and s["down_n"] == 2
    assert s["nasdaq"] is not None                         # QQQ rides the stat circles
    assert s["r52"]["CCC"]["close"] == 103.0               # live price on the gauge


def test_too_few_core_holdings_is_reported_not_drawn():
    s = compute_summary(brief_records(SNAPS, DATA), ["AAA"], "2026-07-17")
    assert s is None
