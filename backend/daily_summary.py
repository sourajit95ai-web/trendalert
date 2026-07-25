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
MONO = "DejaVu Sans Mono"                     # matplotlib-bundled; no font install
# callout panel palettes: (strong, edge, fill)
ACT_COL = ("#8A5A00", "#E0A83E", "#FDF3E0")   # action (amber)
RE_COL = ("#1E5FA5", "#7FB0F8", "#EAF2FD")    # re-entry (blue)
GOLD_COL = ("#146B4A", "#4FB286", "#E7F5EE")  # golden cross (green)
DEATH_COL = ("#A32B27", "#E8827E", "#FDECEB")  # death cross (red)
MAX_ROWS = 6                                  # per panel, so the PNG can't run away


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


def session_label(et):
    """Eastern wall-clock -> which run this is, stamped in the heading badge.

    Lets the same alert say what it is at a glance: the 08:35 job reads
    PRE-MARKET, the 16:50 job MARKET CLOSE. DST is already handled upstream
    by trading_calendar._eastern_now.
    """
    hm = et.hour * 60 + et.minute
    if hm < 9 * 60:
        return "PRE-MARKET"
    if hm < 9 * 60 + 30:
        return "30 MIN TO OPEN"
    if hm < 12 * 60:
        return "MORNING SESSION"
    if hm < 15 * 60:
        return "MIDDAY"
    if hm < 16 * 60:
        return "POWER HOUR"
    return "MARKET CLOSE"


def _crosses(alerts):
    """data.json 'alerts' -> (golden, death) row lists.

    Same EMA50/EMA150 events the dashboard's Recent Alerts cards show — read
    straight off the published payload rather than recomputed here.
    """
    gold, death = [], []
    for a in (alerts or []):
        if not isinstance(a, dict):
            continue
        sym = a.get("symbol")
        if not sym:
            continue
        row = (sym, a.get("type") or a.get("detail") or "EMA cross")
        d = a.get("dir")
        if d == "bull":
            gold.append(row)
        elif d == "bear":
            death.append(row)
        else:                                  # no dir — infer from the wording
            (gold if "above" in row[1].lower() else death).append(row)
    return gold, death


def _callout_groups(s):
    """Ordered [(tag, shown_rows, colors, total)] — ONE tag per group.

    Shared by the chart panels and the caption so the two can never drift.
    Long groups are capped at MAX_ROWS with a '+N more' row; the header count
    always reports the true total.
    """
    out = []
    for tag, rows, col in (("⚡ ACTION", s.get("action"), ACT_COL),
                           ("◎ RE-ENTRY", s.get("reentry"), RE_COL),
                           ("▲ GOLDEN CROSS", s.get("golden"), GOLD_COL),
                           ("▼ DEATH CROSS", s.get("death"), DEATH_COL)):
        rows = list(rows or [])
        if not rows:
            continue
        shown = rows[:MAX_ROWS]
        if len(rows) > MAX_ROWS:
            shown = shown + [("", f"+{len(rows) - MAX_ROWS} more")]
        out.append((tag, shown, col, len(rows)))
    return out


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
                    positions=None, hz=2.0, lz=10.0, gain_pct=20.0,
                    alerts=None, session=""):
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

    golden, death = _crosses(alerts)

    row = lambda r: (r["symbol"], round(r.get("change_pct") or 0, 1), _badge(r))
    dsym = lambda r: r["symbol"].replace("/USD", "")
    r52 = {r["symbol"]: _r52(r) for r in up + down}
    r52.update({dsym(r): _r52(r) for r in idx})
    return {
        "core_name": core_name, "label": label, "session": session,
        "up": [row(r) for r in up], "down": [row(r) for r in down],
        "idx": [(dsym(r), round(r.get("change_pct") or 0, 1), _badge(r)) for r in idx],
        "action": action, "reentry": reentry, "r52": r52,
        "golden": golden, "death": death,
        "breadth": f"{up_n} up · {down_n} down"
                   + (f" · {ext} at 52-week extremes" if ext else ""),
    }


def summary_text(s):
    """Telegram caption + email text fallback.

    Deliberately NOT a transcript of the chart: the movers / index / 52-week
    numbers are already legible in the PNG, so the text carries only what needs
    reading and reasoning about — the grouped action, re-entry and cross
    signals, under one header per group.
    """
    head = (s.get("session") or "").title()
    lines = [f"TrendAlert Daily · {head}" if head else "TrendAlert Daily",
             f"{s['core_name']} · {s['label']}"]
    for tag, rows, _col, total in _callout_groups(s):
        lines += ["", f"{tag} ({total})"]
        lines += [f"  {sym:<5} {why}" if sym else f"  {why}" for sym, why in rows]
    if len(lines) == 2:
        lines += ["", "No action, re-entry or cross signals today."]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# render (matplotlib -> PNG bytes)
# ----------------------------------------------------------------------
PANEL_PAD = 1.55        # header + breathing room, in row-height units


def _callout_height(groups):
    """Callout block size in 'line units' — panels grow with their row count."""
    return sum(len(rows) + PANEL_PAD for _t, rows, _c, _n in groups) \
        + 0.45 * max(len(groups) - 1, 0)


def _draw_callout(ax, groups, L, plt, FancyBboxPatch):
    """Tinted panel per group: colored left edge, ONE header, then the rows."""
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, L)
    top = L
    for tag, rows, (strong, edge, fill), total in groups:
        h = len(rows) + PANEL_PAD
        ax.add_patch(FancyBboxPatch(
            (0.006, top - h + 0.18), 0.988, h - 0.36,
            boxstyle="round,pad=0,rounding_size=0.012", mutation_aspect=0.06,
            fc=fill, ec=edge, lw=1.1, zorder=1, clip_on=False))
        ax.add_patch(plt.Rectangle((0.006, top - h + 0.18), 0.007, h - 0.36,
                                   fc=strong, ec="none", zorder=2, clip_on=False))
        ax.text(0.032, top - 0.82, f"{tag}   ({total})", va="center", ha="left",
                fontsize=9.5, fontweight="bold", color=strong, zorder=3)
        for i, (sym, why) in enumerate(rows):
            ry = top - PANEL_PAD - i - 0.5
            ax.text(0.05, ry, sym, va="center", ha="left", fontsize=9.5,
                    fontweight="bold", color="#111", family=MONO, zorder=3)
            ax.text(0.135, ry, why, va="center", ha="left", fontsize=9.5,
                    color="#1f1f1f", zorder=3)
        top -= h + 0.45


def render_chart_png(s):
    """1-day move bars (left) beside a 52-week range gauge (right).

    Each gauge is a lo ●———— hi track with the real prices labeled at the ends
    and a marker where the current close sits; it glows amber within 2% of the
    52w high (book-profit watch) and blue within 2% of the low. Zebra banding
    ties the two columns together, values ride in solid chips, and numbers are
    monospaced so the columns align. Above the chart, one tinted panel per
    signal group (action / re-entry / golden cross / death cross) — a single
    header each, never a tag repeated per row. The session badge (PRE-MARKET,
    MARKET CLOSE, …) sits top-right so the run is identifiable at a glance.
    """
    import matplotlib
    matplotlib.use("Agg")                   # headless; imported lazily so the pure
    import matplotlib.pyplot as plt          # logic (and tests) need no matplotlib

    from matplotlib.gridspec import GridSpec
    from matplotlib.patches import FancyBboxPatch

    movers = sorted(s["up"] + s["down"], key=lambda m: m[1], reverse=True)
    idx = sorted(s["idx"], key=lambda m: m[1], reverse=True)
    meta = s.get("r52", {})
    groups = _callout_groups(s)
    L = _callout_height(groups)
    ch = 0.30 * L + 0.30

    # one stacked axis: movers, a labelled spacer, then indexes
    rows = [("m", m) for m in movers] + [("gap", None)] + [("i", m) for m in idx]
    n = len(rows)
    mx = max((abs(m[1]) for m in movers + idx), default=1) or 1

    fig = plt.figure(dpi=150, figsize=(9.8, 0.5 * n + ch + 1.75))
    if groups:
        gs = GridSpec(2, 2, height_ratios=[ch, 0.5 * n],
                      width_ratios=[1.0, 1.32], hspace=0.15, wspace=0.03)
        ax_c = fig.add_subplot(gs[0, :])
        axL, axR = fig.add_subplot(gs[1, 0]), fig.add_subplot(gs[1, 1])
    else:
        gs = GridSpec(1, 2, width_ratios=[1.0, 1.32], wspace=0.03)
        ax_c = None
        axL, axR = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])

    ys = [n - 1 - i for i in range(n)]

    # zebra banding, drawn under both columns so rows read straight across
    for ax in (axL, axR):
        ax.set_ylim(-0.6, n - 0.4)
        for j, (y, (kind, m)) in enumerate(zip(ys, rows)):
            if m is not None and j % 2 == 0:
                ax.axhspan(y - 0.5, y + 0.5, color="#f2f3f5", zorder=0)

    # ---- left: 1-day move bars ----
    for y, (kind, m) in zip(ys, rows):
        if m is None:
            axL.text(-mx * 1.35, y, "INDEXES", va="center", ha="left", fontsize=8.5,
                     color="#fff", fontweight="bold",
                     bbox=dict(boxstyle="round,pad=0.3", fc="#333", ec="none"))
            continue
        v = m[1]
        axL.barh(y, v, color=UP if v >= 0 else DOWN, height=0.5, zorder=3)
        axL.text(v + (mx * 0.03 if v >= 0 else -mx * 0.03), y, f"{v:+.1f}%",
                 va="center", ha="left" if v >= 0 else "right", fontsize=9,
                 fontweight="bold", color="white", family=MONO,
                 bbox=dict(boxstyle="round,pad=0.22",
                           fc=UP if v >= 0 else DOWN, ec="none"))
    axL.axvline(0, color="#222", lw=1.1, zorder=2)
    axL.set_xlim(-mx * 1.4, mx * 1.4)
    axL.set_yticks([y for y, (k, m) in zip(ys, rows) if m is not None])
    axL.set_yticklabels([m[0] for (k, m) in rows if m is not None], fontsize=11.5,
                        fontweight="bold", color="#111")
    for sp in ("top", "right", "left"):
        axL.spines[sp].set_visible(False)
    axL.tick_params(length=0)
    axL.set_xticks([])
    axL.set_title("1-DAY MOVE", loc="left", fontsize=9.5, color="#333",
                  fontweight="bold", pad=8)

    # ---- right: 52-week range gauges ----
    axR.set_xlim(0, 1)
    axR.set_title("52-WEEK RANGE", loc="left", fontsize=9.5, color="#333",
                  fontweight="bold", pad=8)
    for sp in axR.spines.values():
        sp.set_visible(False)
    # column divider as this axis's own spine — can't bleed into the callout
    axR.spines["left"].set_visible(True)
    axR.spines["left"].set_color("#d5d7db")
    axR.spines["left"].set_linewidth(1.0)
    axR.tick_params(length=0)
    axR.set_xticks([])
    axR.set_yticks([])
    gx0, gx1 = 0.24, 0.80
    for y, (kind, m) in zip(ys, rows):
        if m is None:
            continue
        mm = meta.get(m[0], {})
        lo, hi, c = mm.get("lo"), mm.get("hi"), mm.get("close")
        axR.plot([gx0, gx1], [y, y], color="#b9bcc2", lw=4.5,
                 solid_capstyle="round", zorder=1)
        ph, pl = mm.get("pfh"), mm.get("pfl")
        frac = (c - lo) / (hi - lo) if (hi and lo and hi > lo and c) else 0.5
        frac = min(max(frac, 0), 1)
        xp = gx0 + frac * (gx1 - gx0)
        near_hi = ph is not None and ph >= -2
        near_lo = pl is not None and pl <= 2
        mc = HI_C if near_hi else LO_C if near_lo else "#111"
        axR.scatter([xp], [y], s=100 if (near_hi or near_lo) else 60, color=mc,
                    zorder=3, edgecolors="white", linewidths=1.5)
        axR.text(gx0 - 0.02, y, _money(lo), va="center", ha="right",
                 fontsize=8.5, color="#333", family=MONO)
        axR.text(gx1 + 0.02, y, _money(hi), va="center", ha="left",
                 fontsize=8.5, color="#333", family=MONO)
        if near_hi:
            axR.text(xp, y + 0.34, f"{abs(ph):.1f}% to hi", va="bottom",
                     ha="center", fontsize=8, fontweight="bold", color=HI_C)
        elif near_lo:
            axR.text(xp, y + 0.34, f"{pl:.1f}% off lo", va="bottom",
                     ha="center", fontsize=8, fontweight="bold", color=LO_C)

    # ---- grouped signal panels, full-width on top ----
    if ax_c is not None:
        _draw_callout(ax_c, groups, L, plt, FancyBboxPatch)

    fig.text(0.015, 0.988, "TrendAlert Daily", ha="left", va="top",
             fontsize=15, fontweight="bold", color="#0d0d0d")
    if s.get("session"):
        fig.text(0.985, 0.984, s["session"], ha="right", va="top", fontsize=9.5,
                 fontweight="bold", color="white",
                 bbox=dict(boxstyle="round,pad=0.42", fc="#111", ec="none"))
    fig.text(0.015, 0.949, f"{s['core_name']} · {s['label']}", ha="left", va="top",
             fontsize=9.5, color="#555", fontweight="bold")
    fig.add_artist(plt.Line2D([0.015, 0.985], [0.934, 0.934], color=UP, lw=2.5,
                              transform=fig.transFigure))
    fig.text(0.015, 0.012,
             s["breadth"] + "    ·    amber = near 52w high · blue = near 52w low",
             fontsize=8.5, color="#333")
    fig.subplots_adjust(top=0.915, bottom=0.05, left=0.08, right=0.985)
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
        label = raw or et.strftime("%b %d")
        session = session_label(et)

        positions = _read_json(bucket, "positions.json", {}) or {}
        if isinstance(positions, dict) and "positions" in positions:
            positions = positions["positions"]
        cfg = _read_json(bucket, "settings.json", {}) or {}
        f = lambda k, d: float(cfg.get(k, d)) if isinstance(cfg, dict) else d

        s = compute_summary(records, load_core(bucket), label, positions=positions,
                            hz=f("highZonePct", 2.0), lz=f("lowZonePct", 10.0),
                            gain_pct=f("gainPct", 20.0),
                            alerts=data.get("alerts"), session=session)
        if s is None:
            return {"ok": True, "summary": "skipped(too-few-core-holdings)"}

        png = render_chart_png(s)
        caption = summary_text(s)
        subject = f"TrendAlert Daily {raw or ''} — {session.title()}".strip()

        statuses = []
        for send in (lambda: send_telegram_photo(png, caption),
                     lambda: send_email_image(subject, caption, png)):
            try:
                statuses.append(send())
            except Exception as e:
                statuses.append(f"error({type(e).__name__})")
        return {"ok": True, "summary": "+".join(statuses),
                "movers": len(s["up"]) + len(s["down"])}
    except Exception as e:
        return {"ok": False, "summary": f"error({type(e).__name__})"}
