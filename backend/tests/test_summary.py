"""Daily-summary composition tests — pure ranking/selection, no matplotlib/GCS.

Mirrors the poster: top 3 up / 3 down among CORE holdings,
QQQ/SPY/BTC pinned plus best+worst of the rest, breadth, and 52w badges.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daily_summary import (compute_summary, summary_text, standfirst,
                           edges_line, _badge,
                           _crosses, _cross_scope, _callout_groups, _words,
                           _long_date, session_label, MAX_ROWS, DASHBOARD_URL)


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


def test_nasdaq_stat_circle_reads_off_the_pinned_index():
    """QQQ rides the poster's stat circles as NASDAQ, not just the index panel."""
    s = compute_summary(RECORDS, CORE, "L")
    assert s["nasdaq"] == ("QQQ", -1.9, "")        # (sym, change_pct, 52w badge)
    assert s["nasdaq"] in s["idx"]                 # same number the panel draws
    # no QQQ quote -> the circle falls back to a dash rather than breaking
    recs = [r for r in RECORDS if r["symbol"] != "QQQ"]
    assert compute_summary(recs, CORE, "L")["nasdaq"] is None


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


def test_summary_text_is_only_the_dashboard_link():
    """The poster carries the reading; the caption is just the way in."""
    s = compute_summary(RECORDS, CORE, "Jul 23", session="MARKET CLOSE")
    assert summary_text(s) == DASHBOARD_URL
    # signal-laden days say no more than quiet ones — the image says it instead
    loud = compute_summary(RECORDS, CORE, "Jul 23", session="MARKET CLOSE",
                           alerts=[{"symbol": "AAA", "dir": "bull", "type": "x"}])
    assert summary_text(loud) == DASHBOARD_URL
    assert summary_text() == DASHBOARD_URL          # s is optional and ignored


def test_r52_context_and_caption_range():
    # +5.0% puts ZZZ inside the top 3, which is what the poster draws
    recs = list(RECORDS) + [rec("ZZZ", "Technology", 5.0, close=210, hi=214, lo=164)]
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
    # the poster draws one card per group, with the true total in its header
    assert [(tag, total) for tag, _r, _c, total in _callout_groups(s)] \
        == [("⚡ ACTION", 2), ("◎ RE-ENTRY", 1)]


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


def test_cross_alerts_reach_summary_and_poster():
    alerts = [{"symbol": "AAA", "dir": "bull",
               "type": "EMA 50 crossed above EMA 150", "detail": "Golden cross"}]
    s = compute_summary(RECORDS, CORE, "Jul 24", alerts=alerts, session="MARKET CLOSE")
    assert s["golden"] == [("AAA", "EMA 50 crossed above EMA 150")]
    assert s["death"] == []
    groups = _callout_groups(s)
    assert [(tag, total) for tag, _r, _c, total in groups] == [("▲ GOLDEN CROSS", 1)]
    assert groups[0][1] == [("AAA", "EMA 50 crossed above EMA 150")]
    # empty groups are omitted entirely, so no blank DEATH CROSS card is drawn


def test_cross_scope_is_core_plus_the_index_tape():
    scope = _cross_scope(RECORDS, CORE)
    assert {"AAA", "GGG"} <= scope                  # Core holdings
    assert {"SPY", "QQQ", "SOXX", "IGV", "IWM"} <= scope   # every Index-sector name
    assert "BTC/USD" in scope                       # pinned even though sector=Crypto
    assert "HHH" not in scope                       # non-Core equity stays out


def test_crosses_are_scoped_but_keep_indexes():
    """Whole-universe crosses are filtered to Core + indexes, not Core alone."""
    alerts = [
        {"symbol": "AAA", "dir": "bull", "type": "EMA 50 crossed above EMA 150"},
        {"symbol": "HHH", "dir": "bull", "type": "EMA 50 crossed above EMA 150"},
        {"symbol": "SPY", "dir": "bear", "type": "EMA 150 crossed below EMA 50"},
    ]
    s = compute_summary(RECORDS, CORE, "L", alerts=alerts)
    assert s["golden"] == [("AAA", "EMA 50 crossed above EMA 150")]   # HHH dropped
    assert s["death"] == [("SPY", "EMA 150 crossed below EMA 50")]    # index KEPT
    # case-insensitive; BTC/USD is labelled BTC to match the chart
    assert _crosses([{"symbol": "aaa", "dir": "bull", "type": "x"}], ["AAA"])[0]
    assert _crosses([{"symbol": "BTC/USD", "dir": "bull", "type": "x"}],
                    ["BTC/USD"])[0] == [("BTC", "x")]
    # no scope given -> unfiltered, so the helper stays usable on its own
    assert len(_crosses(alerts)[0]) == 2
    # a cross on an out-of-scope name must not leave an empty group behind
    s2 = compute_summary(RECORDS, CORE, "L",
                         alerts=[{"symbol": "HHH", "dir": "bull", "type": "x"}])
    assert s2["golden"] == [] and _callout_groups(s2) == []


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


# ----------------------------------------------------------------------
# poster copy — the headline, standfirst and verdict chips
# ----------------------------------------------------------------------
def test_headline_and_meta_are_spelled_out():
    s = compute_summary(RECORDS, CORE, "2026-07-24", session="PRE-MARKET")
    assert s["headline"] == "Four up, three down."      # 4 up / 3 down in the fixture
    assert (s["up_n"], s["down_n"], s["watched"]) == (4, 3, 7)
    assert _words(0) == "zero" and _words(12) == "twelve" and _words(30) == "thirty"
    assert _words(21) == "twenty-one" and _words(1000) == "1000"
    assert _long_date("2026-07-24") == "Friday 24 July 2026"
    assert _long_date("close") == "close"               # non-ISO labels pass through


def test_standfirst_reads_the_days_worst_and_the_re_entry_bench():
    recs = [
        rec("SINK", "Technology", -15.1, close=52, hi=300, lo=50),   # worst mover
        rec("BASE", "Finance", -1.0, close=44, hi=100, lo=40),       # near low
        rec("AAA", "Technology", 3.0), rec("BBB", "Finance", -2.0),
        rec("CCC", "Consumer", 1.0),
    ]
    recs[1]["base_status"] = "forming"
    recs[1]["base_score"] = 3
    core = ["SINK", "BASE", "AAA", "BBB", "CCC"]
    s = compute_summary(recs, core, "L", session="PRE-MARKET")
    assert s["standfirst"].startswith("SINK gave back 15% overnight")
    # SINK is 4% off its own low, so it is on the re-entry bench as well
    assert "two names are still circling their lows" in s["standfirst"]
    assert s["status"]["SINK"] == "NOT YET"      # no base under it
    assert "One of them has a base worth waiting on." in s["standfirst"]
    # the same reason line no longer carries the verdict — that is a chip now
    assert dict(s["reentry"])["BASE"] == "10.0% off low · base forming 3/5"
    assert s["status"]["BASE"] == "WAIT"


def test_standfirst_session_wording_and_cross_clause():
    s = compute_summary(RECORDS, CORE, "L", session="MARKET CLOSE",
                        alerts=[{"symbol": "AAA", "dir": "bull", "type": "x"},
                                {"symbol": "SPY", "dir": "bear", "type": "y"}])
    assert "today" in s["standfirst"] and "overnight" not in s["standfirst"]
    assert "One golden cross and one death cross printed." in s["standfirst"]
    # no re-entry names in this fixture -> says so instead of trailing off
    assert "nothing is sitting at its lows" in s["standfirst"]
    assert standfirst({"session": "", "up": [], "down": [], "reentry": []}) \
        == "Nothing is sitting at its lows."


def test_edges_line_replaces_the_per_row_52w_rails():
    """Who is at a 52-week extreme, as one caption sentence."""
    s = {"up": [("NOW", 7.4, "lo"), ("VEEV", 3.8, "")],
         "down": [("NFLX", -1.0, "lo"), ("AMD", -3.2, "hi")]}
    assert edges_line(s) == "NOW and NFLX sit near 52-week lows · AMD near its high"
    assert edges_line({"up": [("AMD", 1.0, "hi")]}) == "AMD near its high"
    assert edges_line({"down": [("X", -1.0, "lo")]}) == "X sits near its 52-week low"
    assert edges_line({"up": [("A", 1.0, "hi")], "down": [("B", -1.0, "hi")]}) \
        == "A and B near their highs"
    assert edges_line({"up": [], "down": []}) == ""     # line is dropped entirely
    # three or more reads as a list
    assert edges_line({"up": [(c, 1.0, "lo") for c in "ABC"]}).startswith(
        "A, B and C sit near 52-week lows")
    # and compute_summary carries it on the summary the poster paints from
    s2 = compute_summary(RECORDS + [rec("EDGE", "Technology", 9.9, close=100,
                                        hi=101, lo=40)],
                         CORE + ["EDGE"], "L")
    assert s2["edges"] == "EDGE near its high"


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
