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


# ----------------------------------------------------------------------
# compute (pure — tested without network/GCS/matplotlib)
# ----------------------------------------------------------------------
def _badge(rec):
    """'hi' within 2% of the 52w high, 'lo' within 2% of the low — the tape rule."""
    hi, lo, close = rec.get("high_252"), rec.get("low_252"), rec.get("close")
    if close and hi and hi > 0 and round((close - hi) / hi * 1000) / 10 >= -2:
        return "hi"
    if close and lo and lo > 0 and round((close - lo) / lo * 1000) / 10 <= 2:
        return "lo"
    return ""


def compute_summary(records, core_syms, label, core_name="Core"):
    """-> summary dict, or None when there are too few holdings to rank."""
    by = {r.get("symbol"): r for r in records if r.get("symbol")}

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

    row = lambda r: (r["symbol"], round(r.get("change_pct") or 0, 1), _badge(r))
    return {
        "core_name": core_name, "label": label,
        "up": [row(r) for r in up], "down": [row(r) for r in down],
        "idx": [(r["symbol"].replace("/USD", ""), round(r.get("change_pct") or 0, 1),
                 _badge(r)) for r in idx],
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

    movers = sorted(s["up"] + s["down"], key=lambda m: m[1], reverse=True)
    syms = [m[0] for m in movers]
    vals = [m[1] for m in movers]
    cols = [UP if v >= 0 else DOWN for v in vals]
    n = len(movers)

    fig, ax = plt.subplots(figsize=(7.2, 0.46 * n + 1.9), dpi=140)
    ys = list(range(n))
    ax.barh(ys, vals, color=cols, height=0.62, zorder=3)
    ax.set_yticks(ys)
    ax.set_yticklabels(syms, fontsize=11)
    ax.invert_yaxis()
    ax.axvline(0, color="#333", lw=0.8, zorder=2)

    mx = max((abs(v) for v in vals), default=1) or 1
    for y, v in zip(ys, vals):
        ax.text(v + (mx * 0.02 if v >= 0 else -mx * 0.02), y, f"{v:+.1f}%",
                va="center", ha="left" if v >= 0 else "right",
                fontsize=9.5, fontweight="bold", color=UP if v >= 0 else DOWN)
    ax.set_xlim(-mx * 1.28, mx * 1.28)

    ax.set_title(f"TrendAlert · {s['core_name']} — {s['label']}",
                 fontsize=13, fontweight="bold", loc="left", pad=12)
    idx_txt = "   ·   ".join(f"{sym} {v:+.1f}" for sym, v, _ in s["idx"])
    fig.text(0.015, 0.015, idx_txt + "\n" + s["breadth"], fontsize=8.5, color="#555")

    ax.grid(axis="x", color="#ececec", lw=0.6, zorder=0)
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    from matplotlib.ticker import FuncFormatter
    ax.xaxis.set_major_formatter(FuncFormatter(lambda t, _: f"{t:+.0f}%" if t else "0"))
    ax.tick_params(length=0)
    ax.tick_params(axis="x", colors="#999", labelsize=8)

    fig.tight_layout(rect=(0, 0.07, 1, 1))
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

        s = compute_summary(records, load_core(bucket), label)
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
