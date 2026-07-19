"""Bloodbath assessment/composition tests — pure logic, no network/GCS.

The alarm must fire only when BOTH indexes breach the gate AND breadth
confirms (or is unmeasurable), grade severity correctly, honour settings
overrides, and compose a message that flags positions gapping under EMA50.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bloodbath import DEFAULTS, load_params, assess, compose_alert


def _q(pct, last=100.0):
    return {"last": last, "prev_close": last / (1 + pct / 100.0), "pct": pct}


SECTORS = {"SPY": "Index", "QQQ": "Index", "IWM": "Index",
           "AAA": "Technology", "BBB": "Technology", "CCC": "Technology",
           "DDD": "Finance", "EEE": "Finance", "FFF": "Healthcare",
           "GGG": "Healthcare", "HHH": "Consumer", "III": "Consumer",
           "JJJ": "Industrial", "KKK": "Industrial"}

# 11 equities (>= minCoverage 10), all deep red -> full-blown bloodbath
CRASH = {"SPY": _q(-3.2), "QQQ": _q(-4.1),
         **{s: _q(-3.0) for s in SECTORS if SECTORS[s] != "Index"}}


def test_params_override_and_junk():
    p = load_params({"bloodbath": {"indexDropPct": 3, "sectorFrac": "junk"}})
    assert p["indexDropPct"] == 3.0
    assert p["sectorFrac"] == DEFAULTS["sectorFrac"]      # junk -> default
    assert load_params({}) == DEFAULTS


def test_quiet_day_no_trigger():
    quotes = dict(CRASH, SPY=_q(-0.5), QQQ=_q(-2.5))
    a = assess(quotes, SECTORS, DEFAULTS)
    assert not a["gate"] and not a["triggered"]           # SPY holds -> no alarm


def test_gate_needs_both_indexes():
    quotes = dict(CRASH, QQQ=_q(-1.9))                    # QQQ misses the -2%
    assert not assess(quotes, SECTORS, DEFAULTS)["triggered"]


def test_crash_triggers_and_grades():
    a = assess(CRASH, SECTORS, DEFAULTS)
    assert a["triggered"] and a["measurable"]
    assert a["tier"] == "BLOODBATH"                       # avg -3.65% and 100% red
    assert a["decl_frac"] == 1.0
    assert len(a["hard_sectors"]) == 5

    deeper = {s: _q(-6.0) if SECTORS.get(s) == "Index" else q
              for s, q in CRASH.items()}
    assert assess(deeper, SECTORS, DEFAULTS)["tier"] == "CRASH WATCH"

    # -2.2% with everything red is still RED OPEN — magnitude leads the tier
    mild = {s: (_q(-2.2) if SECTORS.get(s) == "Index" else _q(-1.6))
            for s in CRASH}
    a3 = assess(mild, SECTORS, DEFAULTS)
    assert a3["triggered"] and a3["tier"] == "RED OPEN"

    # -2.6% avg AND >=90% red escalates without reaching -3%
    near = {s: (_q(-2.6) if SECTORS.get(s) == "Index" else _q(-1.6))
            for s in CRASH}
    assert assess(near, SECTORS, DEFAULTS)["tier"] == "BLOODBATH"


def test_breadth_veto():
    # indexes down hard but the universe is green -> idiosyncratic, no alarm
    quotes = {s: (_q(-2.5) if SECTORS.get(s) == "Index" else _q(+0.4))
              for s in CRASH}
    a = assess(quotes, SECTORS, DEFAULTS)
    assert a["gate"] and not a["triggered"]


def test_thin_premarket_gate_alone_fires():
    quotes = {"SPY": _q(-2.5), "QQQ": _q(-2.8),
              "AAA": _q(+0.5), "BBB": _q(-0.2)}           # 2 < minCoverage 10
    a = assess(quotes, SECTORS, DEFAULTS)
    assert not a["measurable"] and a["triggered"]


def test_compose_sections_and_ema_flag():
    a = assess(CRASH, SECTORS, DEFAULTS)
    positions = {"AAA": {"entry": 80.0}, "ZZZ": {"entry": 5.0}}   # ZZZ: no quote
    data = [{"symbol": "AAA", "ema50": 105.0}]            # AAA last 100 < 105
    txt = compose_alert(a, CRASH, SECTORS, positions, data, "Mon Jul 20, 08:30 ET")
    assert "BLOODBATH" in txt and "INDEXES" in txt and "PLAYBOOK" in txt
    assert "SPY" in txt and "5/5 sectors down hard" in txt
    aaa = next(l for l in txt.splitlines() if l.startswith("AAA"))
    assert "⚠ opening under EMA50" in aaa and "+25.0% since entry" in aaa
    assert "ZZZ" not in txt


def test_compose_no_positions_fallback():
    a = assess(CRASH, SECTORS, DEFAULTS)
    txt = compose_alert(a, CRASH, SECTORS, {}, [], "Mon Jul 20, 08:30 ET")
    assert "no tracked positions" in txt
