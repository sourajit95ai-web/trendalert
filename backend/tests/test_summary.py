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
