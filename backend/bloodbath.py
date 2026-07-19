"""
bloodbath.py — pre-open crash alarm, checked 1 hour before the bell.

Triggered by a dedicated Cloud Scheduler job hitting the pipeline function
with ?mode=bloodbath at 8:30 AM America/New_York (Mon-Fri). Unlike the
morning brief it is SILENT on normal days — it only delivers when the
market is gapping down hard across the board:

  GATE      SPY and QQQ both <= -indexDropPct pre-market vs prior close
  BREADTH   confirmed by EITHER >= sectorFrac of measurable sectors
            averaging <= -sectorDropPct, OR >= declinerFrac of names with
            fresh pre-market prints in the red. With fewer than minCoverage
            fresh quotes breadth is unmeasurable and the gate alone fires
            (flagged in the message — thin IEX pre-market).

Severity tiers (avg of the SPY/QQQ drop):
  RED OPEN    gate met (-2 .. -3%)
  BLOODBATH   avg <= -3%  or >= 90% of names red
  CRASH WATCH avg <= -5%  (S&P level-1 circuit breaker halts at -7%)

Prices are IEX pre-market prints (thin, indicative — not executable levels).
Thresholds are overridable from published settings.json under "bloodbath".
Delivery reuses the EOD channels (alerts_email.py). Never raises — the
scheduler must always get a 200.
"""

import requests

from alerts_email import _read_json, send_email_text, send_telegram_text
from trading_calendar import is_trading_day, _eastern_now

SNAPSHOT_URL = "https://data.alpaca.markets/v2/stocks/snapshots"
FEAR_PROXY = "VIXY"          # volatility ETF — reported as context, never gating

DEFAULTS = {
    "indexDropPct": 2.0,     # SPY AND QQQ both down at least this much
    "sectorDropPct": 1.5,    # a sector is "down hard" at <= -this avg
    "sectorFrac": 0.7,       # breadth: fraction of sectors down hard
    "declinerFrac": 0.75,    # breadth: fraction of fresh names in the red
    "minCoverage": 10,       # fresh quotes needed before breadth is trusted
}


def load_params(settings):
    """Published settings.json {"bloodbath": {...}} over DEFAULTS."""
    cfg = settings.get("bloodbath") if isinstance(settings, dict) else None
    out = dict(DEFAULTS)
    if isinstance(cfg, dict):
        for k in out:
            try:
                if k in cfg:
                    out[k] = float(cfg[k])
            except (TypeError, ValueError):
                pass                       # a junk override falls back to default
    return out


# ----------------------------------------------------------------------
# fetch
# ----------------------------------------------------------------------
def fetch_premarket(symbols, headers, today):
    """Pre-open quotes -> {sym: {last, prev_close, pct}}.

    Uses latestTrade (IEX includes pre-market prints from 8:00 ET) against
    the prior session's close. Before any trade today, dailyBar is still
    yesterday's bar — so the reference close is prevDailyBar.c only when
    dailyBar has rolled to today, else dailyBar.c. Symbols without a trade
    stamped today are dropped (stale overnight prints must not count)."""
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
        trade = snap.get("latestTrade") or {}
        day = snap.get("dailyBar") or {}
        prev = snap.get("prevDailyBar") or {}
        last = trade.get("p")
        if not last or str(trade.get("t", ""))[:10] != today:
            continue
        ref = prev.get("c") if str(day.get("t", ""))[:10] == today else day.get("c")
        if not ref:
            continue
        pct = (float(last) - float(ref)) / float(ref) * 100.0
        out[sym] = {"last": float(last), "prev_close": float(ref),
                    "pct": round(pct, 2)}
    return out


# ----------------------------------------------------------------------
# decide (pure — tested without network/GCS)
# ----------------------------------------------------------------------
def assess(quotes, sector_of, params):
    """-> assessment dict; ["triggered"] says whether to alert at all."""
    p = params
    spy, qqq = quotes.get("SPY"), quotes.get("QQQ")
    gate = (spy is not None and qqq is not None
            and spy["pct"] <= -p["indexDropPct"]
            and qqq["pct"] <= -p["indexDropPct"])

    equities = {s: q for s, q in quotes.items()
                if sector_of.get(s, "Other") not in ("Index", "Crypto")
                and s != FEAR_PROXY}
    coverage = len(equities)
    red = sum(1 for q in equities.values() if q["pct"] < 0)
    decl_frac = red / coverage if coverage else 0.0

    by = {}
    for s, q in equities.items():
        by.setdefault(sector_of.get(s, "Other"), []).append(q["pct"])
    sectors = {sec: {"avg": round(sum(v) / len(v), 2), "n": len(v),
                     "down": sum(1 for x in v if x < 0)}
               for sec, v in by.items()}
    hard = [sec for sec, st in sectors.items() if st["avg"] <= -p["sectorDropPct"]]
    sector_frac = len(hard) / len(sectors) if sectors else 0.0

    measurable = coverage >= p["minCoverage"]
    breadth = (not measurable
               or sector_frac >= p["sectorFrac"]
               or decl_frac >= p["declinerFrac"])

    avg_idx = (spy["pct"] + qqq["pct"]) / 2.0 if gate else 0.0
    if avg_idx <= -5.0:
        tier = "CRASH WATCH"
    elif avg_idx <= -3.0 or (avg_idx <= -2.5 and measurable and decl_frac >= 0.9):
        tier = "BLOODBATH"                 # deep move, or near-deep with ~everything red
    else:
        tier = "RED OPEN"

    return {"triggered": gate and breadth, "gate": gate, "tier": tier,
            "avg_idx": round(avg_idx, 2), "sectors": sectors,
            "hard_sectors": sorted(hard), "sector_frac": round(sector_frac, 2),
            "decliners": red, "coverage": coverage,
            "decl_frac": round(decl_frac, 2), "measurable": measurable}


# ----------------------------------------------------------------------
# compose (pure)
# ----------------------------------------------------------------------
def compose_alert(a, quotes, sector_of, positions, data_records, now_label):
    """-> multi-line text: severity, indexes, sectors, exposed positions."""
    ema50_of = {r.get("symbol"): r.get("ema50") for r in data_records or []}

    lines = [f"🩸 TrendAlert {a['tier']} — pre-open alert, {now_label}",
             "(IEX pre-market prints — thin and indicative, not executable)", ""]

    lines.append("INDEXES — pre-market vs prior close")
    for s in sorted(s for s in quotes if sector_of.get(s) == "Index"):
        lines.append(f"{s}  ${quotes[s]['last']:,.2f} · {quotes[s]['pct']:+.2f}%")
    if FEAR_PROXY in quotes:
        lines.append(f"{FEAR_PROXY} {quotes[FEAR_PROXY]['pct']:+.2f}% (fear proxy)")
    lines.append("")

    lines.append(f"SECTORS — {a['decliners']}/{a['coverage']} names red"
                 if a["measurable"] else
                 "SECTORS — too few pre-market prints to measure breadth")
    for sec, st in sorted(a["sectors"].items(), key=lambda kv: kv[1]["avg"]):
        mark = " ⬇" if sec in a["hard_sectors"] else ""
        lines.append(f"{sec}: {st['avg']:+.2f}% avg · "
                     f"{st['down']}/{st['n']} down{mark}")
    if a["hard_sectors"]:
        lines.append(f"{len(a['hard_sectors'])}/{len(a['sectors'])} sectors down hard")
    lines.append("")

    lines.append("YOUR POSITIONS — exposure at the open")
    tracked = [s for s in positions if s in quotes and positions[s].get("entry")]
    if tracked:
        for s in sorted(tracked, key=lambda x: quotes[x]["pct"]):
            q = quotes[s]
            entry = float(positions[s]["entry"])
            since = (q["last"] - entry) / entry * 100.0
            e50 = ema50_of.get(s)
            flag = " · ⚠ opening under EMA50" if e50 and q["last"] < float(e50) else ""
            lines.append(f"{s}  ${q['last']:,.2f} · {q['pct']:+.2f}% pre-market "
                         f"· {since:+.1f}% since entry{flag}")
    else:
        lines.append("no tracked positions with pre-market prints")
    lines += ["",
              "PLAYBOOK",
              "- Do nothing in the first 15 min; let the opening range set",
              "- Gaps below EMA50 on booked names: trail-exit rule applies on the CLOSE, not the open",
              "- Panic opens often mark the low of the day — review, don't market-sell"]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# orchestrator
# ----------------------------------------------------------------------
def run_bloodbath_check(bucket, symbols, sectors, headers):
    """Never raises. -> dict for the HTTP response."""
    try:
        et = _eastern_now()
        if not is_trading_day(et.date()):
            return {"ok": True, "bloodbath": "skipped(non-trading-day)"}
        today = et.date().isoformat()

        # universe = core + UI-added equities; sector falls back to data.json
        extras = _read_json(bucket, "universe.json", []) or []
        data = _read_json(bucket, "data.json", {}) or {}
        sector_of = dict(sectors)
        for rec in data.get("symbols", []):
            sector_of.setdefault(rec.get("symbol"), rec.get("sector", "Other"))
        wanted = sorted({s.strip().upper() for s in symbols + list(extras)
                         if isinstance(s, str) and s.strip() and "/" not in s}
                        | {FEAR_PROXY})

        quotes = fetch_premarket(wanted, headers, today)
        if "SPY" not in quotes or "QQQ" not in quotes:
            return {"ok": True, "bloodbath": "skipped(no-premarket-index-prints)"}

        params = load_params(_read_json(bucket, "settings.json", {}) or {})
        a = assess(quotes, sector_of, params)
        if not a["triggered"]:
            return {"ok": True, "bloodbath": f"quiet(spy {quotes['SPY']['pct']:+.2f}%"
                                            f", qqq {quotes['QQQ']['pct']:+.2f}%)"}

        positions = _read_json(bucket, "positions.json", {}) or {}
        if isinstance(positions, dict) and "positions" in positions:
            positions = positions["positions"]

        now_label = et.strftime("%a %b %d, %H:%M ET")
        body = compose_alert(a, quotes, sector_of, positions,
                             data.get("symbols", []), now_label)

        statuses = []
        for send in (lambda: send_telegram_text(body),
                     lambda: send_email_text(
                         f"🩸 TrendAlert {a['tier']} {today} — "
                         f"SPY {quotes['SPY']['pct']:+.1f}% QQQ {quotes['QQQ']['pct']:+.1f}%",
                         body)):
            try:
                statuses.append(send())
            except Exception as e:
                statuses.append(f"error({type(e).__name__})")
        return {"ok": True, "bloodbath": "+".join(statuses), "tier": a["tier"]}
    except Exception as e:
        return {"ok": False, "bloodbath": f"error({type(e).__name__})"}
