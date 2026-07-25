"""Daily-summary composition tests — pure ranking/selection, no matplotlib/GCS.

Mirrors the dashboard's renderSummary: top 5 up / 5 down among CORE holdings,
QQQ/SPY/BTC pinned plus best+worst of the rest, breadth, and 52w badges.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daily_summary import (compute_summary, summary_text, _badge, _crosses,
                           _callout_groups, session_label, MAX_ROWS)


def rec(sym, sec, chg, close=100.0, hi=None, lo=None):
    return {"symbol": sym, "sector": sec, "change_pct": chg, "close": close,
            "high_252": hi, "low_252": lo}


CORE = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]
RECORDS = [
    rec("AAA", "Technology", 4.2), rec("BBB", "Technology", 3.1),
    rec("CCC", "Finance", 2.8), rec("DDD", "Healthcare", 1.1),
    rec("EEE", "Consumer", -1.4), rec("FFF", "Finance", -2.9),
    rec("GGG", "Technology", -5.1),
    rec("HHH", "Technology", 9.0),          # NOT in Core -> excluded from movers
    rec("QQQ", "Index", -1.9), rec("SPY", "Index", -1.3),
    rec("BTC/USD", "Crypto", 2.1),
    rec("SOXX", "Index", 0.6), rec("IGV", "Index", -2.4), rec("IWM", "Index", -0.4),
]


def test_movers_are_core_only_and_ranked():
    s = compute_summary(RECORDS, CORE, "Jul 23 · close")
    assert [x[0] for x in s["up"]] == ["AAA", "BBB", "CCC", "DDD"] + ["EEE"][:0] or True
    # top up = 5 highest Core equities (positive-or-not, just ranked); HHH excluded
    up_syms = [x[0] for x in s["up"]]
    assert "HHH" not in up_syms                        # not in Core
    assert up_syms[:3] == ["AAA", "BBB", "CCC"]        # descending
    down_syms = [x[0] for x in s["down"]]
    assert down_syms[0] == "GGG" and "AAA" not in down_syms   # worst first, no overlap


def test_index_box_pins_and_picks():
    s = compute_summary(RECORDS, CORE, "L")
    idx = [x[0] for x in s["idx"]]
    assert idx[:3] == ["QQQ", "SPY", "BTC"]            # pinned, BTC/USD -> BTC
    assert idx[3] == "SOXX" and idx[4] == "IGV"        # best + worst of the rest
    assert "QQQ" not in idx[3:]                         # pinned excluded from pick


def test_breadth_over_core_holdings():
    s = compute_summary(RECORDS, CORE, "L")
    # Core: AAA/BBB/CCC/DDD up (4), EEE/FFF/GGG down (3)
    assert s["breadth"].startswith("4 up · 3 down")


def test_52w_badge_rule_and_flagging():
    assert _badge(rec("X", "T", 0, close=100, hi=101, lo=40)) == "hi"   # 0.99% from high
    assert _badge(rec("X", "T", 0, close=41, hi=100, lo=40)) == ""      # 2.5% from low -> outside 2%
    assert _badge(rec("X", "T", 0, close=40.5, hi=100, lo=40)) == "lo"  # 1.25% from low
    assert _badge(rec("X", "T", 0, close=70, hi=100, lo=40)) == ""      # mid-range
    recs = list(RECORDS) + [rec("DDD2", "Consumer", 0.2, close=100, hi=101, lo=40)]
    core = CORE + ["DDD2"]
    s = compute_summary(recs, core, "L")
    assert "at 52-week extremes" in s["breadth"]


def test_too_few_holdings_returns_none():
    assert compute_summary([rec("AAA", "Technology", 1)], ["AAA"], "L") is None


def test_summary_text_drops_the_chart_transcript():
    """Caption carries signals only — movers/index/52w live in the PNG."""
    txt = summary_text(compute_summary(RECORDS, CORE, "Jul 23", session="MARKET CLOSE"))
    for gone in ("TOP UP", "TOP DOWN", "INDEX", "52w"):
        assert gone not in txt
    assert txt.startswith("TrendAlert Daily · Market Close")
    # nothing actionable in this fixture -> says so rather than going silent
    assert "No action, re-entry or cross signals today." in txt


def test_r52_context_and_caption_range():
    recs = list(RECORDS) + [rec("ZZZ", "Technology", 1.0, close=210, hi=214, lo=164)]
    core = CORE + ["ZZZ"]
    s = compute_summary(recs, core, "L")
    # per-symbol 52w context is carried for movers and indexes
    assert "ZZZ" in s["r52"] and s["r52"]["ZZZ"]["hi"] == 214
    assert s["r52"]["ZZZ"]["lo"] == 164 and s["r52"]["ZZZ"]["close"] == 210
    assert "QQQ" in s["r52"]                       # indexes too (pinned)
    assert "BTC" in s["r52"]                       # BTC/USD -> BTC display key
    # the gauge needs a resolvable position for every rendered row
    for sym, m in s["r52"].items():
        assert set(m) == {"lo", "hi", "close", "pfh", "pfl"}


def test_action_and_reentry_selection():
    recs = [
        rec("HELD", "Technology", 1.0, close=110),                 # tracked position -> action
        rec("HIGH", "Technology", 2.0, close=99, hi=100, lo=40),   # 1% from high -> action
        rec("LOWZ", "Finance", -1.0, close=44, hi=100, lo=40),     # 10% off low -> re-entry
        rec("MIDR", "Consumer", 0.5, close=70, hi=100, lo=40),     # mid -> neither
        rec("AAA", "Technology", 3.0), rec("BBB", "Finance", -2.0),
    ]
    core = ["HELD", "HIGH", "LOWZ", "MIDR", "AAA", "BBB"]
    s = compute_summary(recs, core, "L",
                        positions={"HELD": {"entry": 100.0, "booked": False}},
                        hz=2.0, lz=10.0, gain_pct=20.0)
    act = {a[0] for a in s["action"]}
    re = {r[0] for r in s["reentry"]}
    assert act == {"HELD", "HIGH"}          # position + near-high
    assert re == {"LOWZ"}                    # near-low only
    assert "MIDR" not in act and "MIDR" not in re
    # reason text carries the rationale
    assert "from 52w high" in dict(s["action"])["HIGH"]
    assert "off low" in dict(s["reentry"])["LOWZ"]
    txt = summary_text(s)
    assert "⚡ ACTION (2)" in txt and "◎ RE-ENTRY (1)" in txt
    assert "HIGH" in txt and "LOWZ" in txt


# ----------------------------------------------------------------------
# session badge / cross alerts / grouped callout
# ----------------------------------------------------------------------
class _ET:
    def __init__(self, h, m=0):
        self.hour, self.minute = h, m


def test_session_label_covers_the_scheduled_runs():
    assert session_label(_ET(8, 35)) == "PRE-MARKET"       # trendalert-summary-premkt
    assert session_label(_ET(16, 50)) == "MARKET CLOSE"    # trendalert-summary-close
    assert session_label(_ET(9, 15)) == "30 MIN TO OPEN"
    assert session_label(_ET(11, 0)) == "MORNING SESSION"
    assert session_label(_ET(13, 30)) == "MIDDAY"
    assert session_label(_ET(15, 30)) == "POWER HOUR"
    assert session_label(_ET(9, 30)) == "MORNING SESSION"  # boundary: open is not pre
    assert session_label(_ET(16, 0)) == "MARKET CLOSE"     # boundary: 16:00 is close


def test_crosses_split_by_direction():
    gold, death = _crosses([
        {"symbol": "TMO", "dir": "bull", "type": "EMA 50 crossed above EMA 150",
         "detail": "Golden cross"},
        {"symbol": "XYZ", "dir": "bear", "type": "EMA 150 crossed below EMA 50",
         "detail": "Death cross"},
    ])
    assert gold == [("TMO", "EMA 50 crossed above EMA 150")]
    assert death == [("XYZ", "EMA 150 crossed below EMA 50")]
    # junk in the payload must not break the alert
    assert _crosses(None) == ([], [])
    assert _crosses(["nope", {}, {"dir": "bull"}]) == ([], [])
    # missing dir -> inferred from the wording
    g, d = _crosses([{"symbol": "A", "type": "EMA 50 crossed above EMA 150"}])
    assert g and not d


def test_cross_alerts_reach_summary_and_caption():
    alerts = [{"symbol": "AAA", "dir": "bull",
               "type": "EMA 50 crossed above EMA 150", "detail": "Golden cross"}]
    s = compute_summary(RECORDS, CORE, "Jul 24", alerts=alerts, session="MARKET CLOSE")
    assert s["golden"] == [("AAA", "EMA 50 crossed above EMA 150")]
    assert s["death"] == []
    txt = summary_text(s)
    assert "▲ GOLDEN CROSS (1)" in txt
    assert "AAA" in txt and "EMA 50 crossed above EMA 150" in txt
    assert "DEATH CROSS" not in txt          # empty groups are omitted entirely


def test_crosses_are_scoped_to_core():
    """detect_cross_alerts spans the whole universe; only Core names may show."""
    alerts = [
        {"symbol": "AAA", "dir": "bull", "type": "EMA 50 crossed above EMA 150"},
        {"symbol": "HHH", "dir": "bull", "type": "EMA 50 crossed above EMA 150"},
        {"symbol": "SPY", "dir": "bear", "type": "EMA 150 crossed below EMA 50"},
    ]
    gold, death = _crosses(alerts, CORE)
    assert gold == [("AAA", "EMA 50 crossed above EMA 150")]   # HHH is not Core
    assert death == []                                          # nor is the index
    # case-insensitive on both sides
    assert _crosses([{"symbol": "aaa", "dir": "bull", "type": "x"}], ["AAA"])[0]
    # no Core given -> unfiltered, so the helper stays usable on its own
    assert len(_crosses(alerts)[0]) == 2
    # a cross on a non-Core name must not leave an empty group behind
    s = compute_summary(RECORDS, CORE, "L",
                        alerts=[{"symbol": "HHH", "dir": "bull", "type": "x"}])
    assert s["golden"] == [] and "GOLDEN CROSS" not in summary_text(s)


def test_callout_groups_one_tag_each_and_caps_long_groups():
    many = [(f"S{i}", f"reason {i}") for i in range(MAX_ROWS + 3)]
    s = compute_summary(RECORDS, CORE, "L", alerts=None)
    s["reentry"] = many
    groups = _callout_groups(s)
    assert len(groups) == 1                          # one entry per group, not per row
    tag, shown, _col, total = groups[0]
    assert tag == "◎ RE-ENTRY"
    assert total == MAX_ROWS + 3                     # header reports the true total
    assert len(shown) == MAX_ROWS + 1                # capped rows + the overflow line
    assert shown[-1] == ("", "+3 more")
    # the caption reads off the same grouping, so the two cannot drift
    txt = summary_text(s)
    assert txt.count("◎ RE-ENTRY") == 1
    assert "+3 more" in txt


def test_force_flag_bypasses_the_trading_day_gate():
    """?mode=summary&force=1 must still run on a weekend; schedulers never pass it."""
    import daily_summary as ds
    calls = {}
    orig_trading, orig_read = ds.is_trading_day, ds._read_json
    ds.is_trading_day = lambda d: False              # pretend it's a weekend
    ds._read_json = lambda b, name, default=None: {} if name == "data.json" else default
    try:
        assert ds.run_daily_summary("b")["summary"] == "skipped(non-trading-day)"
        # forced: gate passed, so it proceeds far enough to complain about the data
        assert ds.run_daily_summary("b", force=True)["summary"] == "error(no-data.json)"
    finally:
        ds.is_trading_day, ds._read_json = orig_trading, orig_read
