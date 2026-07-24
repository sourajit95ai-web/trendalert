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
    return {
        "core_name": core_name, "label": label,
        "up": [row(r) for r in up], "down": [row(r) for r in down],
        "idx": [(r["symbol"].replace("/USD", ""), round(r.get("change_pct") or 0, 1),
                 _badge(r)) for r in idx],
        "action": action, "reentry": reentry,
        "breadth": f"{up_n} up · {down_n} down"
                   + (f" · {ext} at 52-week extremes" if ext else ""),
    }


def summary_text(s):
    """Plain-text rendering — Telegram caption + email text fallback."""
    tag = lambda b: " [52w hi]" if b == "hi" else " [52w lo]" if b == "lo" else ""
    lines = [f"TrendAlert Daily — {s['core_name']} · {s['label']}", "", "TOP UP"]
    lines += [f"  {sym:<6}{v:+.1f}%{tag(b)}" for sym, v, b in s["up"]]
    lines += ["TOP DOWN"]
    lines += [f"  {sym:<6}{v:+.1f}%{tag(b)}" for sym, v, b in s["down"]]
    if s.get("action") or s.get("reentry"):
        lines += [""]
        lines += [f"⚡ ACTION {sym} — {why}" for sym, why in s.get("action", [])]
        lines += [f"◎ RE-ENTRY {sym} — {why}" for sym, why in s.get("reentry", [])]
    lines += ["", "INDEX  " + "  ".join(f"{sym} {v:+.1f}" for sym, v, _ in s["idx"]),
              s["breadth"]]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# render (matplotlib -> PNG bytes)
# ----------------------------------------------------------------------
def render_chart_png(s):
    import matplotlib
    matplotlib.use("Agg")                   # headless; imported lazily so the pure
    import matplotlib.pyplot as plt          # logic (and tests) need no matplotlib

    from matplotlib.ticker import FuncFormatter
    from matplotlib.gridspec import GridSpec

    movers = sorted(s["up"] + s["down"], key=lambda m: m[1], reverse=True)
    idx = sorted(s["idx"], key=lambda m: m[1], reverse=True)
    callout = [("⚡ ACTION", sym, why, ACT_COL) for sym, why in s.get("action", [])] \
        + [("◎ RE-ENTRY", sym, why, RE_COL) for sym, why in s.get("reentry", [])]
    nm, ni, nc = len(movers), max(len(idx), 1), len(callout)
    # shared scale across the bar panels so index bars read as "moved less"
    mx = max((abs(m[1]) for m in movers + idx), default=1) or 1

    ratios = ([nc * 0.62] if nc else []) + [nm, ni]
    fig = plt.figure(dpi=140,
                     figsize=(7.2, 0.42 * (nm + ni) + 0.30 * nc + 2.6))
    gs = GridSpec(len(ratios), 1, height_ratios=ratios, hspace=0.34)
    r = 0
    ax_c = fig.add_subplot(gs[r]) if nc else None
    r += 1 if nc else 0
    ax_m = fig.add_subplot(gs[r])
    ax_i = fig.add_subplot(gs[r + 1], sharex=ax_m)

    def draw(ax, rows):
        vals = [x[1] for x in rows]
        cols = [UP if v >= 0 else DOWN for v in vals]
        ys = list(range(len(rows)))
        ax.barh(ys, vals, color=cols, height=0.6, zorder=3)
        ax.set_yticks(ys)
        ax.set_yticklabels([x[0] for x in rows], fontsize=10.5)
        ax.invert_yaxis()
        ax.axvline(0, color="#333", lw=0.8, zorder=2)
        for y, v in zip(ys, vals):
            ax.text(v + (mx * 0.02 if v >= 0 else -mx * 0.02), y, f"{v:+.1f}%",
                    va="center", ha="left" if v >= 0 else "right",
                    fontsize=9, fontweight="bold", color=UP if v >= 0 else DOWN)
        ax.grid(axis="x", color="#ececec", lw=0.6, zorder=0)
        for sp in ("top", "right", "left"):
            ax.spines[sp].set_visible(False)
        ax.tick_params(length=0)

    draw(ax_m, movers)
    draw(ax_i, idx)
    ax_m.set_xlim(-mx * 1.28, mx * 1.28)          # shared via sharex
    ax_i.set_title("INDEXES", loc="left", fontsize=8.5, color="#999",
                   fontweight="bold", pad=6)
    ax_m.tick_params(labelbottom=False)
    ax_i.xaxis.set_major_formatter(FuncFormatter(lambda t, _: f"{t:+.0f}%" if t else "0"))
    ax_i.tick_params(axis="x", colors="#999", labelsize=8)

    if ax_c is not None:
        ax_c.axis("off")
        ax_c.set_xlim(0, 1)
        ax_c.set_ylim(0, 1)
        for i, (chip, sym, why, (fc, ec, tc)) in enumerate(callout):
            y = 1 - (i + 0.5) / nc
            ax_c.text(0.004, y, f"{chip}  {sym}", transform=ax_c.transAxes,
                      va="center", ha="left", fontsize=8.5, fontweight="bold",
                      color=tc, bbox=dict(boxstyle="round,pad=0.32", fc=fc, ec=ec, lw=0.9))
            ax_c.text(0.40, y, why, transform=ax_c.transAxes, va="center",
                      ha="left", fontsize=8.5, color="#666")

    fig.text(0.015, 0.985, f"TrendAlert · {s['core_name']} — {s['label']}",
             ha="left", va="top", fontsize=13, fontweight="bold")
    fig.text(0.015, 0.012, s["breadth"], fontsize=8.5, color="#555")
    fig.subplots_adjust(top=0.90, bottom=0.06, left=0.13, right=0.97)
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
