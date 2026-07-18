"""
morning_brief.py — market-open snapshot pushed 20 min into the session.

Triggered by a dedicated Cloud Scheduler job hitting the pipeline function
with ?mode=brief at 9:50 AM America/New_York (Mon-Fri). Unlike the EOD
pipeline it does NOT recompute scores or overwrite data.json — it reads
intraday snapshots and paints the whole picture of the day so far:

  INDEXES   how the day started — opening gap vs yesterday's close, drift
            since the open, and the running day move
  SECTORS   average first-20-min move per sector across the universe, with
            the best / worst name called out per sector
  YOUR STOCKS  tracked positions (positions.json) with today's move, sector,
            and gain since entry — the "how are MY names doing" line

Delivery reuses the EOD channels (alerts_email.py): one email + one Telegram
message per configured recipient. Either channel can be disabled the same
way as EOD alerts. Never raises — the scheduler must always get a 200.
"""

import json
from datetime import datetime

import requests

from alerts_email import _read_json, send_email_text, send_telegram_text
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


def _fmt_line(sym, q):
    gap = _pct(q["open"], q["prev_close"])
    drift = _pct(q["last"], q["open"])
    day = _pct(q["last"], q["prev_close"])
    return (f"{sym}  ${q['last']:,.2f} · opened {gap:+.2f}% "
            f"· since open {drift:+.2f}% · day {day:+.2f}%")


def compose_brief(snaps, sector_of, positions, now_label):
    """-> multi-line text: indexes, sector behaviour, tracked positions."""
    day_of = {s: _pct(q["last"], q["prev_close"]) for s, q in snaps.items()}

    idx = sorted(s for s in snaps if sector_of.get(s) == "Index")
    equities = [s for s in snaps if sector_of.get(s, "Other") not in ("Index", "Crypto")]

    lines = [f"TrendAlert Morning Brief — {now_label}",
             "(20 min into the session — IEX intraday, not settled closes)", ""]

    lines.append("INDEXES — how the day started")
    if idx:
        lines += [_fmt_line(s, snaps[s]) for s in idx]
    else:
        lines.append("no index snapshots available")
    lines.append("")

    lines.append("SECTORS — first 20 minutes, across your universe")
    by = {}
    for s in equities:
        by.setdefault(sector_of.get(s, "Other"), []).append(s)
    for sec in sorted(by, key=lambda k: -sum(day_of[s] for s in by[k]) / len(by[k])):
        syms = by[sec]
        avg = sum(day_of[s] for s in syms) / len(syms)
        up = sum(1 for s in syms if day_of[s] >= 0)
        best = max(syms, key=lambda s: day_of[s])
        worst = min(syms, key=lambda s: day_of[s])
        line = (f"{sec}: {avg:+.2f}% avg · {up} up / {len(syms) - up} down "
                f"· best {best} {day_of[best]:+.2f}%")
        if worst != best:
            line += f" · worst {worst} {day_of[worst]:+.2f}%"
        lines.append(line)
    lines.append("")

    lines.append("YOUR STOCKS — tracked positions")
    tracked = [s for s in positions if s in snaps and positions[s].get("entry")]
    if tracked:
        for s in sorted(tracked, key=lambda x: -day_of[x]):
            q = snaps[s]
            since = _pct(q["last"], float(positions[s]["entry"]))
            booked = " · ⅓ booked" if positions[s].get("booked") else ""
            lines.append(f"{s}  ${q['last']:,.2f} · day {day_of[s]:+.2f}% "
                         f"· {sector_of.get(s, 'Other')} "
                         f"· {since:+.1f}% since entry ${float(positions[s]['entry']):,.2f}{booked}")
    else:
        lines.append("no tracked positions (set entries in the dashboard to see them here)")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# orchestrator
# ----------------------------------------------------------------------
def run_morning_brief(bucket, symbols, sectors, headers):
    """Never raises. -> dict for the HTTP response."""
    try:
        et = _eastern_now()
        if not is_trading_day(et.date()):
            return {"ok": True, "brief": "skipped(non-trading-day)"}

        # universe = core + UI-added equities; sector falls back to data.json
        extras = _read_json(bucket, "universe.json", []) or []
        sector_of = dict(sectors)
        for rec in (_read_json(bucket, "data.json", {}) or {}).get("symbols", []):
            sector_of.setdefault(rec.get("symbol"), rec.get("sector", "Other"))
        wanted = sorted({s.strip().upper() for s in symbols + list(extras)
                         if isinstance(s, str) and s.strip() and "/" not in s})

        snaps = fetch_snapshots(wanted, headers)
        if not snaps:
            return {"ok": False, "brief": "error(no-snapshots)"}
        # stale bars (weekend restarts, feed hiccups) — every bar must be today's
        today = et.date().isoformat()
        snaps = {s: q for s, q in snaps.items() if q["bar_date"] == today}
        if not snaps:
            return {"ok": True, "brief": "skipped(no-intraday-bars-yet)"}

        positions = _read_json(bucket, "positions.json", {}) or {}
        if isinstance(positions, dict) and "positions" in positions:
            positions = positions["positions"]

        now_label = et.strftime("%a %b %d, %H:%M ET")
        body = compose_brief(snaps, sector_of, positions, now_label)

        statuses = []
        for send in (lambda: send_telegram_text(body),
                     lambda: send_email_text(f"TrendAlert Morning Brief {today}", body)):
            try:
                statuses.append(send())
            except Exception as e:
                statuses.append(f"error({type(e).__name__})")
        return {"ok": True, "brief": "+".join(statuses), "symbols": len(snaps)}
    except Exception as e:
        return {"ok": False, "brief": f"error({type(e).__name__})"}
