"""Morning-brief composition tests — pure text assembly, no network/GCS.

The brief must always carry the three sections (indexes / sectors / your
stocks), compute gap-vs-drift-vs-day correctly, and degrade gracefully when
there are no index snapshots or no tracked positions.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morning_brief import compose_brief, _pct


SNAPS = {
    # prev 100 -> opened 102 (gap +2%) -> now 101 (drift -0.98%, day +1%)
    "AAA": {"last": 101.0, "open": 102.0, "prev_close": 100.0, "bar_date": "2026-07-17"},
    "BBB": {"last": 95.0, "open": 99.0, "prev_close": 100.0, "bar_date": "2026-07-17"},
    "CCC": {"last": 103.0, "open": 100.5, "prev_close": 100.0, "bar_date": "2026-07-17"},
    "SPY": {"last": 500.0, "open": 498.0, "prev_close": 495.0, "bar_date": "2026-07-17"},
}
SECTORS = {"AAA": "Technology", "BBB": "Technology", "CCC": "Finance", "SPY": "Index"}
LABEL = "Fri Jul 17, 09:50 ET"


def test_pct():
    assert _pct(102, 100) == 2.0
    assert _pct(100, 0) == 0.0


def test_sections_present():
    txt = compose_brief(SNAPS, SECTORS, {}, LABEL)
    assert "INDEXES" in txt and "SECTORS" in txt and "YOUR STOCKS" in txt
    assert LABEL in txt


def test_index_line_math():
    txt = compose_brief(SNAPS, SECTORS, {}, LABEL)
    # SPY: gap (498-495)/495=+0.61%, drift (500-498)/498=+0.40%, day +1.01%
    spy = next(l for l in txt.splitlines() if l.startswith("SPY"))
    assert "opened +0.61%" in spy and "since open +0.40%" in spy and "day +1.01%" in spy


def test_sector_aggregation():
    txt = compose_brief(SNAPS, SECTORS, {}, LABEL)
    tech = next(l for l in txt.splitlines() if l.startswith("Technology"))
    # AAA +1%, BBB -5% -> avg -2%, 1 up / 1 down, best AAA, worst BBB
    assert "-2.00% avg" in tech and "1 up / 1 down" in tech
    assert "best AAA" in tech and "worst BBB" in tech
    # Finance has a single name -> no separate worst
    fin = next(l for l in txt.splitlines() if l.startswith("Finance"))
    assert "worst" not in fin


def test_positions_and_fallback():
    txt = compose_brief(SNAPS, SECTORS, {}, LABEL)
    assert "no tracked positions" in txt

    pos = {"AAA": {"entry": 80.0, "booked": True},
           "ZZZ": {"entry": 10.0}}          # no snapshot -> must be skipped
    txt2 = compose_brief(SNAPS, SECTORS, pos, LABEL)
    aaa = next(l for l in txt2.splitlines() if l.startswith("AAA") and "entry" in l)
    # (101-80)/80 = +26.25% -> +26.2% since entry, booked flag shown
    assert "+26.2% since entry $80.00" in aaa and "⅓ booked" in aaa
    assert "ZZZ" not in txt2


def test_index_section_degrades():
    snaps = {k: v for k, v in SNAPS.items() if k != "SPY"}
    txt = compose_brief(snaps, SECTORS, {}, LABEL)
    assert "no index snapshots available" in txt
