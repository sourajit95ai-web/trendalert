"""merge_universe tests — the additive fetch-universe guard.

The bug this fixes: universe.json was overwritten with whatever the current
browser's localStorage held, so opening the dashboard from a device that
lacked a UI-added ticker silently deleted it from the pipeline's fetch list.
merge_universe unions instead, so incoming can only ADD, never remove.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notes_function import merge_universe

DEFAULTS = ["META", "MSFT", "NVDA", "QQQ", "SPY", "BTC/USD"]


def test_incoming_adds_new_tickers():
    out = merge_universe(DEFAULTS, DEFAULTS + ["CRSP", "NU"])
    assert "CRSP" in out and "NU" in out
    assert all(s in out for s in DEFAULTS)


def test_default_browser_cannot_delete_added_tickers():
    # stored universe already has CRSP/NU; a fresh browser posts only defaults
    stored = DEFAULTS + ["CRSP", "NU"]
    out = merge_universe(stored, DEFAULTS)          # incoming lacks CRSP/NU
    assert "CRSP" in out and "NU" in out            # must survive — the whole point


def test_dedupe_and_normalize():
    out = merge_universe(["msft", " nvda "], ["MSFT", "NU", "nu"])
    assert out.count("MSFT") == 1 and out.count("NU") == 1
    assert out == ["MSFT", "NVDA", "NU"]            # order preserved, upper-cased


def test_rejects_junk_symbols():
    out = merge_universe([], ["CRSP", "", "  ", "bad ticker!", "N U", "NU"])
    assert out == ["CRSP", "NU"]                    # blanks + spaces/invalid chars dropped


def test_non_list_inputs_are_safe():
    assert merge_universe({}, ["CRSP"]) == ["CRSP"]   # stored {} (empty/missing)
    assert merge_universe(["CRSP"], None) == ["CRSP"]


def test_cap_bounds_growth():
    big = [f"SYM{i}" for i in range(400)]
    assert len(merge_universe(big, ["CRSP"])) == 250
