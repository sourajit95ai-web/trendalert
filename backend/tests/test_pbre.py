"""clean_pbre tests — PB/RE markers survive the round trip as counts.

The bug this fixes: the handler coerced pb and re to 1, so a name booked three
times came back from pbre.json as pb=1. The dashboard pulls that file on load,
which means the tally was not merely mis-stored — it was reset on the next
refresh, every time.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notes_function import clean_pbre, PBRE_MAX


def test_counts_survive():
    out = clean_pbre({"NVDA": {"pb": 3, "re": 1}})
    assert out == {"NVDA": {"pb": 3, "re": 1}}      # the whole point


def test_flag_map_from_the_shipped_dashboard_still_loads():
    # v2 writes 0/1; 1 is just a count of one, so nothing migrates
    out = clean_pbre({"MSFT": {"pb": 1}, "TMO": {"pb": 1, "re": 1}})
    assert out == {"MSFT": {"pb": 1}, "TMO": {"pb": 1, "re": 1}}


def test_zero_and_empty_entries_are_dropped():
    out = clean_pbre({"AAPL": {"pb": 0, "re": 0}, "META": {}, "GOOG": {"pb": 2}})
    assert out == {"GOOG": {"pb": 2}}               # no empty {} left behind


def test_counts_are_clamped():
    out = clean_pbre({"A": {"pb": -4}, "B": {"pb": 10 ** 6}})
    assert "A" not in out                           # negative floors to 0, then dropped
    assert out["B"]["pb"] == PBRE_MAX


def test_junk_values_read_as_zero():
    out = clean_pbre({"A": {"pb": "two"}, "B": {"pb": None}, "C": {"pb": "3"}})
    assert "A" not in out and "B" not in out
    assert out["C"]["pb"] == 3                      # numeric strings still count


def test_non_dict_inputs_are_safe():
    assert clean_pbre(None) == {}
    assert clean_pbre([1, 2]) == {}
    assert clean_pbre({"A": "pb"}) == {}


def test_symbol_and_map_caps():
    assert list(clean_pbre({"X" * 40: {"pb": 1}})) == ["X" * 16]
    big = {f"SYM{i}": {"pb": 1} for i in range(600)}
    assert len(clean_pbre(big)) == 500
