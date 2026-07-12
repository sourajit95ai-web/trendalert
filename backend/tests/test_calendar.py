"""Tests for trading_calendar.py and alerts_email.detect_events / transition_filter."""
import sys, os
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trading_calendar import (
    is_trading_day, market_holidays, last_completed_trading_day, _easter)
from alerts_email import detect_events, transition_filter


# ---------------- calendar ----------------
def test_weekends_closed():
    assert not is_trading_day(date(2026, 7, 11))   # Saturday
    assert not is_trading_day(date(2026, 7, 12))   # Sunday
    assert is_trading_day(date(2026, 7, 13))       # Monday


def test_fixed_and_observed_holidays():
    assert not is_trading_day(date(2026, 1, 1))    # New Year's (Thu)
    assert not is_trading_day(date(2026, 12, 25))  # Christmas (Fri)
    # Jul 4 2026 is a Saturday -> observed Fri Jul 3
    assert not is_trading_day(date(2026, 7, 3))
    # Juneteenth 2027 is a Saturday -> observed Fri Jun 18
    assert not is_trading_day(date(2027, 6, 18))


def test_floating_holidays_2026():
    assert not is_trading_day(date(2026, 1, 19))   # MLK 3rd Mon Jan
    assert not is_trading_day(date(2026, 2, 16))   # Presidents 3rd Mon Feb
    assert not is_trading_day(date(2026, 5, 25))   # Memorial last Mon May
    assert not is_trading_day(date(2026, 9, 7))    # Labor 1st Mon Sep
    assert not is_trading_day(date(2026, 11, 26))  # Thanksgiving 4th Thu Nov


def test_good_friday():
    assert _easter(2026) == date(2026, 4, 5)
    assert not is_trading_day(date(2026, 4, 3))    # Good Friday 2026
    assert not is_trading_day(date(2025, 4, 18))   # Good Friday 2025


def test_last_completed_trading_day_weekend_and_intraday():
    # Sunday noon UTC -> Friday
    sun = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    assert last_completed_trading_day(sun) == date(2026, 7, 10)
    # Monday 14:00 UTC (10:00 ET, market open, close not settled) -> Friday
    mon_open = datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc)
    assert last_completed_trading_day(mon_open) == date(2026, 7, 10)
    # Monday 21:35 UTC (17:35 EDT, after settle buffer) -> Monday
    mon_close = datetime(2026, 7, 13, 21, 35, tzinfo=timezone.utc)
    assert last_completed_trading_day(mon_close) == date(2026, 7, 13)


# ---------------- alert events ----------------
REC_BOOK = {"symbol": "NVDA", "close": 128.0, "ema50": 120.0,
            "pct_from_high": -0.9, "base_status": "none"}
REC_EXIT = {"symbol": "AAPL", "close": 218.0, "ema50": 221.4,
            "pct_from_high": -6.0, "base_status": "none"}
REC_BASE = {"symbol": "TSLA", "close": 250.0, "ema50": 245.0,
            "pct_from_high": -29.6, "base_status": "confirmed", "base_score": 5}
SETTINGS = {"gainPct": 20, "highZonePct": 2}


def test_book_trigger_fires_on_both_conditions():
    ev = detect_events([REC_BOOK], {"NVDA": {"entry": 103.2, "booked": False}}, SETTINGS)
    assert [e["event"] for e in ev] == ["book"]
    # gain alone is not enough: same gain but 5% off the high
    far = dict(REC_BOOK, pct_from_high=-5.0)
    assert detect_events([far], {"NVDA": {"entry": 103.2, "booked": False}}, SETTINGS) == []


def test_trail_exit_only_when_booked_and_below_ema50():
    ev = detect_events([REC_EXIT], {"AAPL": {"entry": 180.0, "booked": True}}, SETTINGS)
    assert [e["event"] for e in ev] == ["trail_exit"]
    assert detect_events([REC_EXIT], {"AAPL": {"entry": 180.0, "booked": False}}, SETTINGS) == []


def test_base_confirmed_only_without_open_position():
    assert [e["event"] for e in detect_events([REC_BASE], {}, SETTINGS)] == ["base_confirmed"]
    assert detect_events([REC_BASE], {"TSLA": {"entry": 200.0}}, SETTINGS) == []


def test_transition_dedupe():
    events = detect_events([REC_BASE], {}, SETTINGS)
    lines, state = transition_filter(events, [], {})           # first run -> mails
    assert len(lines) == 1 and state == {"TSLA": "base_confirmed"}
    lines2, _ = transition_filter(events, [], state)           # unchanged -> silent
    assert lines2 == []
    crosses = [{"symbol": "MSFT", "type": "EMA 50 crossed above EMA 150", "detail": "Golden cross"}]
    lines3, _ = transition_filter(events, crosses, state)      # crosses always pass
    assert len(lines3) == 1 and "MSFT" in lines3[0]


# ---------------- new-listing / short-history guard ----------------
def _synth(n, seed=11):
    import numpy as np, pandas as pd
    rng = np.random.default_rng(seed)
    close = 50 * np.exp(np.cumsum(rng.normal(0.004, 0.02, n)))
    idx = pd.bdate_range(end=pd.offsets.BDay().rollback(pd.Timestamp.today()), periods=n)
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close,
                         "volume": rng.integers(8e5, 5e6, n).astype(float)}, index=idx)


def test_new_listing_publishes_no_fake_52w_levels():
    from chart_backend import enrich_summary_records
    rec = enrich_summary_records({"IPO": _synth(90)})["IPO"]
    assert rec["limited_history"] is True and rec["history_bars"] == 90
    # the whole point: no 52w level, therefore no zone, therefore booking stays disarmed
    assert rec["high_252"] is None and rec["low_252"] is None
    assert rec["zone"] is None
    assert rec["base_status"] == "insufficient_history"


def test_full_history_symbol_unaffected():
    from chart_backend import enrich_summary_records
    rec = enrich_summary_records({"OLD": _synth(300)})["OLD"]
    assert rec["limited_history"] is False
    assert rec["high_252"] is not None and rec["low_252"] is not None


def test_unseasoned_emas_are_null():
    import sys, types
    for m, attrs in [("google", {}), ("google.cloud", {}), ("google.cloud.storage", {"Client": object})]:
        if m not in sys.modules:
            mod = types.ModuleType(m)
            for k, v in attrs.items():
                setattr(mod, k, v)
            sys.modules[m] = mod
    from main import indicator_record
    rec = indicator_record("IPO", _synth(90))
    assert rec["ema20"] is not None and rec["ema50"] is not None   # 90 bars seasons these
    assert rec["ema150"] is None and rec["ema200"] is None         # these it cannot


def test_book_alert_cannot_fire_without_52w_data():
    # a new listing with a real gain but no pct_from_high must NOT trigger BOOK
    rec = {"symbol": "IPO", "close": 90.0, "ema50": 80.0,
           "pct_from_high": None, "base_status": "insufficient_history"}
    ev = detect_events([rec], {"IPO": {"entry": 50.0, "booked": False}}, SETTINGS)
    assert ev == []
