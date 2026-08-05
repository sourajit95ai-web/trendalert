"""
morning_brief.py — market-open snapshot pushed 20 min into the session.

Triggered by a dedicated Cloud Scheduler job hitting the pipeline function
with ?mode=brief at 9:50 AM America/New_York (Mon-Fri). Unlike the EOD
pipeline it does NOT recompute scores or overwrite data.json — it reads
intraday snapshots and reports the day so far.

PRESENTATION: identical to the market-close alert. This module does not draw
anything of its own — it shapes intraday snapshots into the record form
daily_summary.compute_summary already expects, then hands them to the SAME
compute + render + deliver path. The two alerts therefore cannot drift: any
change to the poster shows up in both, and the only difference is the session
badge (MORNING SESSION vs MARKET CLOSE) and the numbers behind it.

It previously composed its own plain-text body with three sections. That is
gone: the text wall looked nothing like the poster alerts, its sector block
spanned the whole universe while the summary was scoped to Core (so the two
could disagree about the same morning), and its "YOUR STOCKS" section read
positions.json — a file no longer written since entry-price tracking was
removed, so every brief ended by pointing at a dashboard feature that does
not exist.

Never raises — the scheduler must always get a 200.
"""

import requests

from alerts_email import (_read_json, alert_on, caption_text_on, fan_out,
                          send_email_image, send_telegram_photo)
from daily_summary import (compute_summary, load_core, render_chart_png,
                           session_label, summary_text)
from trading_calendar import is_trading_day, _eastern_now

SNAPSHOT_URL = "https://data.alpaca.markets/v2/stocks/snapshots"


# ----------------------------------------------------------------------
# fetch
# ----------------------------------------------------------------------
def fetch_snapshots(symbols, headers):
    """Intraday snapshots -> {sym: {last, open, prev_close, bar_date}}.

    Uses today's dailyBar (o = today's open, c = latest intraday close) and
    prevDailyBar.c. Symbols without a snapshot are silently dropped."""
    out = {}
    if not symbols:
        return out
    r = requests.get(SNAPSHOT_URL, headers=headers,
                     params={"symbols": ",".join(symbols), "feed": "iex"},
                     timeout=30)
    r.raise_for_status()
    for sym, snap in (r.json() or {}).items():
        if not isinstance(snap, dict):
            continue
        day = snap.get("dailyBar") or {}
        prev = snap.get("prevDailyBar") or {}
        if not day.get("c") or not prev.get("c"):
            continue
        out[sym] = {
            "last": float(day["c"]),
            "open": float(day.get("o") or day["c"]),
            "prev_close": float(prev["c"]),
            "bar_date": str(day.get("t", ""))[:10],
        }
    return out


# ----------------------------------------------------------------------
# compose (pure — tested without network/GCS)
# ----------------------------------------------------------------------
def _pct(a, b):
    return (a - b) / b * 100.0 if b else 0.0


def brief_records(snaps, data_records):
    """Intraday snapshots -> the record shape compute_summary consumes.

    The poster needs `symbol`, `change_pct`, `sector` and the 52-week trio
    (`close`, `low_252`, `high_252`). Only the first two come from the live
    snapshot; sector and the 52-week bounds are carried over from data.json,
    which the EOD pipeline already maintains and which does not move
    intraday. `close` is deliberately the LIVE price, not data.json's settled
    one, so the range gauge marks where the name is trading right now.

    change_pct is the move against yesterday's settled close — the same
    quantity the close alert plots, measured at a different hour.

    Symbols in data.json without a snapshot are dropped rather than carried
    at yesterday's price: a stale row next to live ones is worse than a
    missing one.
    """
    meta = {r.get("symbol"): r for r in (data_records or []) if r.get("symbol")}
    out = []
    for sym, q in snaps.items():
        base = meta.get(sym, {})
        out.append({
            "symbol": sym,
            "change_pct": _pct(q["last"], q["prev_close"]),
            "sector": base.get("sector", "Other"),
            "close": q["last"],
            "low_252": base.get("low_252"),
            "high_252": base.get("high_252"),
        })
    return out


# ----------------------------------------------------------------------
# orchestrator
# ----------------------------------------------------------------------
def run_morning_brief(bucket, symbols, sectors, headers, force=False):
    """Never raises. -> dict for the HTTP response.

    Deliberately the same shape as run_daily_summary, including force: it
    bypasses the trading-day gate so the alert can be fired by hand outside
    market hours to eyeball the real output. Schedulers never pass it.
    """
    try:
        et = _eastern_now()
        if not is_trading_day(et.date()) and not force:
            return {"ok": True, "brief": "skipped(non-trading-day)"}
        cfg = _read_json(bucket, "settings.json", {}) or {}
        if not alert_on(cfg, "brief"):
            return {"ok": True, "brief": "skipped(off:brief)"}

        data = _read_json(bucket, "data.json", {}) or {}
        data_records = data.get("symbols", [])

        # fetch the union of the pipeline's universe and the UI-added extras,
        # then let compute_summary scope the poster to Core — same as the
        # close alert, so the two can no longer disagree about a morning
        extras = _read_json(bucket, "universe.json", []) or []
        wanted = sorted({s.strip().upper() for s in list(symbols) + list(extras)
                         if isinstance(s, str) and s.strip() and "/" not in s})

        snaps = fetch_snapshots(wanted, headers)
        if not snaps:
            return {"ok": False, "brief": "error(no-snapshots)"}
        # stale bars (weekend restarts, feed hiccups) — every bar must be today's
        today = et.date().isoformat()
        snaps = {s: q for s, q in snaps.items() if q["bar_date"] == today}
        if not snaps:
            return {"ok": True, "brief": "skipped(no-intraday-bars-yet)"}

        # sectors arg is the pipeline's static map; data.json fills the rest
        records = brief_records(snaps, data_records)
        for r in records:
            if r["sector"] == "Other" and r["symbol"] in sectors:
                r["sector"] = sectors[r["symbol"]]

        session = session_label(et)
        f = lambda k, d: float(cfg.get(k, d)) if isinstance(cfg, dict) else d
        s = compute_summary(records, load_core(bucket), today,
                            hz=f("highZonePct", 2.0), lz=f("lowZonePct", 10.0),
                            gain_pct=f("gainPct", 20.0),
                            alerts=data.get("alerts"), session=session)
        if s is None:
            return {"ok": True, "brief": "skipped(too-few-core-holdings)"}

        png = render_chart_png(s)
        caption = summary_text(s, caption_text_on(cfg))
        subject = f"TrendAlert Daily {today} — {session.title()}"

        status = fan_out(cfg, (
            ("telegram", lambda: send_telegram_photo(png, caption)),
            ("email", lambda: send_email_image(subject, caption, png,
                                               settings=cfg)),
        ))
        return {"ok": True, "brief": status, "symbols": len(snaps)}
    except Exception as e:
        return {"ok": False, "brief": f"error({type(e).__name__})"}
