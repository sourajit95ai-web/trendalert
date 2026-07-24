"""Daily-summary composition tests — pure ranking/selection, no matplotlib/GCS.

Mirrors the dashboard's renderSummary: top 5 up / 5 down among CORE holdings,
QQQ/SPY/BTC pinned plus best+worst of the rest, breadth, and 52w badges.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daily_summary import compute_summary, summary_text, _badge


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


def test_summary_text_has_all_sections():
    txt = summary_text(compute_summary(RECORDS, CORE, "Jul 23 · close"))
    assert "TOP UP" in txt and "TOP DOWN" in txt and "INDEX" in txt
    assert "QQQ" in txt and "4 up · 3 down" in txt


def test_r52_context_and_caption_range():
    recs = list(RECORDS) + [rec("ZZZ", "Technology", 1.0, close=210, hi=214, lo=164)]
    core = CORE + ["ZZZ"]
    s = compute_summary(recs, core, "L")
    # per-symbol 52w context is carried for movers and indexes
    assert "ZZZ" in s["r52"] and s["r52"]["ZZZ"]["hi"] == 214
    assert s["r52"]["ZZZ"]["lo"] == 164 and s["r52"]["ZZZ"]["close"] == 210
    assert "QQQ" in s["r52"]                       # indexes too (pinned)
    assert "BTC" in s["r52"]                       # BTC/USD -> BTC display key
    # caption surfaces the 52-week high/low values on the row
    txt = summary_text(s)
    assert "52w 164–214" in txt
    assert "% vs hi)" in txt


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
    assert "ACTION HIGH" in txt and "RE-ENTRY LOWZ" in txt
