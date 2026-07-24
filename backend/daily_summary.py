"""
daily_summary.py — Core-portfolio EOD summary pushed as a CHART IMAGE.

Triggered by Cloud Scheduler hitting the pipeline function with ?mode=summary
(pre-market ~08:35 ET and after the close ~16:50 ET). It does NOT recompute or
overwrite data.json — it reads the published close and paints the same picture
as the dashboard's daily-summary card, then delivers it as a PNG:

  MOVERS   top 5 up / 5 down among the CORE portfolio holdings
  INDEX    QQQ / SPY / BTC pinned, plus the best + worst of the rest
  BREADTH  up / down count + 52-week extremes across the holdings

Core membership comes from core.json (published by the dashboard, union-merged
server-side so a stale browser can't shrink it); falls back to CORE_SYMBOLS.
Delivery reuses the EOD channels: a Telegram photo + an email with the chart
inlined (alerts_email.send_telegram_photo / send_email_image). Never raises.
"""

import io

from alerts_email import (_read_json, send_telegram_photo, send_email_image)
from trading_calendar import is_trading_day, _eastern_now

# fallback Core if core.json is absent — mirrors the dashboard's CORE_SYMBOLS
CORE_SYMBOLS = [
    "META", "MELI", "MSFT", "GOOGL", "AMZN", "MA", "NFLX", "NVDA", "AMD", "NOW",
    "V", "UNH", "COIN", "VEEV", "HOOD", "TSM", "CRWD", "PANW", "AXON", "LLY",
    "TMO", "NBIS", "AVGO", "ISRG", "PLTR", "RKLB", "IONQ", "NVO",
]
PINNED_IDX = ["QQQ", "SPY", "BTC/USD"]
UP, DOWN = "#1C7A56", "#D8433F"
HI_C, LO_C = "#B45309", "#1D4ED8"             # near 52w high (amber) / low (blue)
ACT_COL = ("#FDF0D5", "#E0A83E", "#8A5A00")   # action chip: fill, edge, text (amber)
RE_COL = ("#E6F0FB", "#7FB0F8", "#1E5FA5")    # re-entry chip (blue)


# ----------------------------------------------------------------------
# compute (pure — tested without network/GCS/matplotlib)
# ----------------------------------------------------------------------
def _pfh(r):
    h, c = r.get("high_252"), r.get("close")
    return (c - h) / h * 100 if (h and c and h > 0) else None


def _pfl(r):
    lo, c = r.get("low_252"), r.get("close")
    return (c - lo) / lo * 100 if (lo and c and lo > 0) else None


def _money(v):
    """Compact price label: 98,231 / 642 / 50.0 / — for missing."""
    if v is None:
        return "—"
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 100:
        return f"{v:.0f}"
    return f"{v:.1f}"


def _r52(rec):
    """52-week context for the range gauge + caption: lo/hi/close and % gaps."""
    return {"lo": rec.get("low_252"), "hi": rec.get("high_252"),
            "close": rec.get("close"), "pfh": _pfh(rec), "pfl": _pfl(rec)}


def _badge(rec):
    """'hi' within 2% of the 52w high, 'lo' within 2% of the low — the tape rule."""
    ph, pl = _pfh(rec), _pfl(rec)
    if ph is not None and round(ph * 10) / 10 >= -2:
        return "hi"
    if pl is not None and round(pl * 10) / 10 <= 2:
        return "lo"
    return ""


def _group(rec, positions, hz, lz):
    """Mirror the dashboard's groupOfCard: 1 = action, 3 = re-entry, 2 = steady."""
    if rec["symbol"] in positions:
        return 1
    if rec.get("limited_history"):
        return 2
    ph, pl = _pfh(rec), _pfl(rec)
    if ph is None:
        return 2
    if ph >= -hz:
        return 1
    if pl is not None and pl <= lz:
        return 3
    return 2


def _action_reason(rec, pos, hz, gain_pct):
    ph = _pfh(rec)
    if pos and pos.get("entry"):
        try:
            entry = float(pos["entry"])
        except (TypeError, ValueError):
            entry = 0
        if entry:
            gain = (rec["close"] - entry) / entry * 100
            near_high = ph is not None and ph >= -hz
            if not pos.get("booked"):
                if gain >= gain_pct and near_high:
                    return f"book ⅓ — +{gain:.1f}% and at high zone"
                return f"tracked +{gain:.1f}% vs +{gain_pct:.0f}%"
            if rec.get("ema50") is not None and rec["close"] < rec["ema50"]:
                return "trail exit — closed below EMA50"
            return "trailing ⅓ — above EMA50"
    return f"{abs(ph):.1f}% from 52w high" if ph is not None else "exit review zone"


def _reentry_reason(rec, lz):
    pl = _pfl(rec)
    bs = rec.get("base_status", "")
    tag = ("base confirmed — re-entry candidate" if bs == "confirmed"
           else f"base forming {rec.get('base_score', '?')}/5 — wait" if bs == "forming"
           else "no base — still falling")
    return (f"{pl:.1f}% off low · " if pl is not None else "") + tag


def compute_summary(records, core_syms, label, core_name="Core",
                    positions=None, hz=2.0, lz=10.0, gain_pct=20.0):
    """-> summary dict, or None when there are too few holdings to rank."""
    by = {r.get("symbol"): r for r in records if r.get("symbol")}
    positions = positions or {}

    def ok(r):
        return r is not None and r.get("change_pct") is not None

    held = [by[s] for s in core_syms
            if s in by and ok(by[s])
            and by[s].get("sector") not in ("Index", "Crypto")]
    if len(held) < 3:
        return None

    key = lambda r: -(r.get("change_pct") or 0)
    eq = sorted(held, key=key)
    up = eq[:5]
    up_set = {r["symbol"] for r in up}
    down = [r for r in reversed(eq) if r["symbol"] not in up_set][:5]

    pinned = [by[s] for s in PINNED_IDX if s in by and ok(by[s])]
    pin_set = {r["symbol"] for r in pinned}
    pool = sorted([r for r in records if ok(r) and r.get("sector") == "Index"
                   and r.get("symbol") not in pin_set], key=key)
    extras = ([pool[0], pool[-1]] if len(pool) > 1 else pool[:1])
    idx = pinned + extras

    up_n = sum(1 for r in held if (r.get("change_pct") or 0) > 0)
    down_n = sum(1 for r in held if (r.get("change_pct") or 0) < 0)
    ext = sum(1 for r in held if _badge(r))

    # action / re-entry over the Core holdings — same rules as the dashboard badges
    action, reentry = [], []
    for sym in core_syms:
        r = by.get(sym)
        if not ok(r):
            continue
        g = _group(r, positions, hz, lz)
        if g == 1:
            action.append((sym, _action_reason(r, positions.get(sym), hz, gain_pct)))
        elif g == 3:
            reentry.append((sym, _reentry_reason(r, lz)))

    row = lambda r: (r["symbol"], round(r.get("change_pct") or 0, 1), _badge(r))
    dsym = lambda r: r["symbol"].replace("/USD", "")
    r52 = {r["symbol"]: _r52(r) for r in up + down}
    r52.update({dsym(r): _r52(r) for r in idx})
    return {
        "core_name": core_name, "label": label,
        "up": [row(r) for r in up], "down": [row(r) for r in down],
        "idx": [(dsym(r), round(r.get("change_pct") or 0, 1), _badge(r)) for r in idx],
        "action": action, "reentry": reentry, "r52": r52,
        "breadth": f"{up_n} up · {down_n} down"
                   + (f" · {ext} at 52-week extremes" if ext else ""),
    }


def summary_text(s):
    """Plain-text rendering — Telegram caption + email text fallback."""
    r52 = s.get("r52", {})

    def rng(sym):
        m = r52.get(sym)
        if not m or m.get("lo") is None or m.get("hi") is None:
            return ""
        gap = f" ({m['pfh']:+.0f}% vs hi)" if m.get("pfh") is not None else ""
        return f"  52w {_money(m['lo'])}–{_money(m['hi'])}{gap}"

    tag = lambda b: " [52w hi]" if b == "hi" else " [52w lo]" if b == "lo" else ""
    lines = [f"TrendAlert Daily — {s['core_name']} · {s['label']}", "", "TOP UP"]
    lines += [f"  {sym:<6}{v:+.1f}%{tag(b)}{rng(sym)}" for sym, v, b in s["up"]]
    lines += ["TOP DOWN"]
    lines += [f"  {sym:<6}{v:+.1f}%{tag(b)}{rng(sym)}" for sym, v, b in s["down"]]
    if s.get("action") or s.get("reentry"):
        lines += [""]
        lines += [f"⚡ ACTION {sym} — {why}" for sym, why in s.get("action", [])]
        lines += [f"◎ RE-ENTRY {sym} — {why}" for sym, why in s.get("reentry", [])]
    lines += ["", "INDEX"]
    lines += [f"  {sym:<6}{v:+.1f}%{rng(sym)}" for sym, v, _ in s["idx"]]
    lines += [s["breadth"]]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# render (matplotlib -> PNG bytes)
# ----------------------------------------------------------------------
def render_chart_png(s):
    """Variant A — 1-day move bars (left) beside a 52-week range gauge (right).

    Each gauge is a lo ●———— hi track with the real prices labeled at the ends
    and a marker where the current close sits; it glows amber within 2% of the
    52w high (book-profit watch) and blue within 2% of the low. An optional
    ACTION / RE-ENTRY callout sits full-width on top.
    """
    import matplotlib
    matplotlib.use("Agg")                   # headless; imported lazily so the pure
    import matplotlib.pyplot as plt          # logic (and tests) need no matplotlib

    from matplotlib.gridspec import GridSpec

    movers = sorted(s["up"] + s["down"], key=lambda m: m[1], reverse=True)
    idx = sorted(s["idx"], key=lambda m: m[1], reverse=True)
    meta = s.get("r52", {})
    callout = [("⚡ ACTION", sym, why, ACT_COL) for sym, why in s.get("action", [])] \
        + [("◎ RE-ENTRY", sym, why, RE_COL) for sym, why in s.get("reentry", [])]
    nc = len(callout)

    # one stacked axis: movers, a labelled spacer, then indexes
    rows = [("m", m) for m in movers] + [("gap", None)] + [("i", m) for m in idx]
    n = len(rows)
    mx = max((abs(m[1]) for m in movers + idx), default=1) or 1

    fig = plt.figure(dpi=150, figsize=(9.6, 0.46 * n + 0.34 * nc + 1.6))
    if nc:
        gs = GridSpec(2, 2, height_ratios=[0.34 * nc + 0.25, 0.46 * n],
                      width_ratios=[1.0, 1.32], hspace=0.14, wspace=0.04)
        ax_c = fig.add_subplot(gs[0, :])
        axL, axR = fig.add_subplot(gs[1, 0]), fig.add_subplot(gs[1, 1])
    else:
        gs = GridSpec(1, 2, width_ratios=[1.0, 1.32], wspace=0.04)
        ax_c = None
        axL, axR = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])

    ys = [n - 1 - i for i in range(n)]

    # ---- left: 1-day move bars ----
    for y, (kind, m) in zip(ys, rows):
        if m is None:
            axL.text(-mx * 1.35, y, "INDEXES", va="center", ha="left", fontsize=7.5,
                     color="#bbb", fontweight="bold")
            continue
        v = m[1]
        axL.barh(y, v, color=UP if v >= 0 else DOWN, height=0.6, zorder=3)
        axL.text(v + (mx * 0.03 if v >= 0 else -mx * 0.03), y, f"{v:+.1f}%",
                 va="center", ha="left" if v >= 0 else "right", fontsize=8.5,
                 fontweight="bold", color=UP if v >= 0 else DOWN)
    axL.axvline(0, color="#333", lw=0.8, zorder=2)
    axL.set_xlim(-mx * 1.35, mx * 1.35)
    axL.set_ylim(-0.6, n - 0.4)
    axL.set_yticks([y for y, (k, m) in zip(ys, rows) if m is not None])
    axL.set_yticklabels([m[0] for (k, m) in rows if m is not None], fontsize=10)
    for sp in ("top", "right", "left"):
        axL.spines[sp].set_visible(False)
    axL.tick_params(length=0)
    axL.set_xticks([])
    axL.set_title("1-day move", loc="left", fontsize=8.5, color="#999",
                  fontweight="bold", pad=6)

    # ---- right: 52-week range gauges ----
    axR.set_xlim(0, 1)
    axR.set_ylim(-0.6, n - 0.4)
    axR.axis("off")
    axR.set_title("52-week range", loc="left", fontsize=8.5, color="#999",
                  fontweight="bold", pad=6)
    gx0, gx1 = 0.20, 0.82
    for y, (kind, m) in zip(ys, rows):
        if m is None:
            continue
        mm = meta.get(m[0], {})
        lo, hi, c = mm.get("lo"), mm.get("hi"), mm.get("close")
        axR.plot([gx0, gx1], [y, y], color="#dcdcdc", lw=3,
                 solid_capstyle="round", zorder=1)
        ph, pl = mm.get("pfh"), mm.get("pfl")
        frac = (c - lo) / (hi - lo) if (hi and lo and hi > lo and c) else 0.5
        frac = min(max(frac, 0), 1)
        xp = gx0 + frac * (gx1 - gx0)
        near_hi = ph is not None and ph >= -2
        near_lo = pl is not None and pl <= 2
        mc = HI_C if near_hi else LO_C if near_lo else "#111"
        axR.scatter([xp], [y], s=76 if (near_hi or near_lo) else 44, color=mc,
                    zorder=3, edgecolors="white", linewidths=1.1)
        axR.text(gx0 - 0.018, y, _money(lo), va="center", ha="right",
                 fontsize=7.5, color="#888")
        axR.text(gx1 + 0.018, y, _money(hi), va="center", ha="left",
                 fontsize=7.5, color="#888")
        if near_hi:
            axR.text(xp, y + 0.36, f"{abs(ph):.1f}% to hi", va="bottom",
                     ha="center", fontsize=7.5, fontweight="bold", color=HI_C)
        elif near_lo:
            axR.text(xp, y + 0.36, f"{pl:.1f}% off lo", va="bottom",
                     ha="center", fontsize=7.5, fontweight="bold", color=LO_C)

    # ---- callout (full-width, on top) ----
    if ax_c is not None:
        ax_c.axis("off")
        ax_c.set_xlim(0, 1)
        ax_c.set_ylim(0, 1)
        for i, (chip, sym, why, (fc, ec, tc)) in enumerate(callout):
            y = 1 - (i + 0.5) / nc
            ax_c.text(0.004, y, f"{chip}  {sym}", transform=ax_c.transAxes,
                      va="center", ha="left", fontsize=8.5, fontweight="bold",
                      color=tc, bbox=dict(boxstyle="round,pad=0.32", fc=fc, ec=ec, lw=0.9))
            ax_c.text(0.33, y, why, transform=ax_c.transAxes, va="center",
                      ha="left", fontsize=8.5, color="#666")

    fig.text(0.015, 0.985, f"TrendAlert · {s['core_name']} — {s['label']}",
             ha="left", va="top", fontsize=13, fontweight="bold")
    fig.text(0.015, 0.012,
             s["breadth"] + "    ·    amber = near 52w high · blue = near 52w low",
             fontsize=8, color="#777")
    fig.subplots_adjust(top=0.90, bottom=0.05, left=0.075, right=0.985)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# ----------------------------------------------------------------------
# orchestrator
# ----------------------------------------------------------------------
def load_core(bucket):
    core = _read_json(bucket, "core.json", None)
    if isinstance(core, list) and core:
        return [str(s).upper() for s in core if isinstance(s, str) and s.strip()]
    return CORE_SYMBOLS


def run_daily_summary(bucket):
    """Never raises. -> dict for the HTTP response."""
    try:
        et = _eastern_now()
        if not is_trading_day(et.date()):
            return {"ok": True, "summary": "skipped(non-trading-day)"}

        data = _read_json(bucket, "data.json", {}) or {}
        records = data.get("symbols", [])
        if not records:
            return {"ok": False, "summary": "error(no-data.json)"}

        raw = data.get("expected_last_trading_day") \
            or (str(data.get("generated_at", ""))[:10] or None)
        label = f"{raw} · close" if raw else et.strftime("%b %d") + " · close"

        positions = _read_json(bucket, "positions.json", {}) or {}
        if isinstance(positions, dict) and "positions" in positions:
            positions = positions["positions"]
        cfg = _read_json(bucket, "settings.json", {}) or {}
        f = lambda k, d: float(cfg.get(k, d)) if isinstance(cfg, dict) else d

        s = compute_summary(records, load_core(bucket), label, positions=positions,
                            hz=f("highZonePct", 2.0), lz=f("lowZonePct", 10.0),
                            gain_pct=f("gainPct", 20.0))
        if s is None:
            return {"ok": True, "summary": "skipped(too-few-core-holdings)"}

        png = render_chart_png(s)
        caption = summary_text(s)

        statuses = []
        for send in (lambda: send_telegram_photo(png, caption),
                     lambda: send_email_image(
                         f"TrendAlert Daily {raw or ''}".strip(), caption, png)):
            try:
                statuses.append(send())
            except Exception as e:
                statuses.append(f"error({type(e).__name__})")
        return {"ok": True, "summary": "+".join(statuses),
                "movers": len(s["up"]) + len(s["down"])}
    except Exception as e:
        return {"ok": False, "summary": f"error({type(e).__name__})"}
