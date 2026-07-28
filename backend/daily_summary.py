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

from alerts_email import (_read_json, alert_on, fan_out,
                          send_telegram_photo, send_email_image)
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


def _cross_scope(records, core_syms):
    """Symbols a cross may name: Core holdings + the benchmark tape.

    detect_cross_alerts runs over the WHOLE universe, so unscoped it returns
    names the alert says nothing else about. Indexes are kept deliberately — a
    cross on SPY/QQQ matters whether or not it was a top mover that day, and
    the chart already has an INDEXES row for them.
    """
    scope = {str(s).upper() for s in (core_syms or [])}
    scope |= {str(r["symbol"]).upper() for r in records
              if r.get("symbol") and r.get("sector") == "Index"}
    scope |= {s.upper() for s in PINNED_IDX}        # QQQ/SPY/BTC-USD are always shown
    return scope


def _crosses(alerts, allowed=None):
    """data.json 'alerts' -> (golden, death) row lists.

    Same EMA50/EMA150 events the dashboard's Recent Alerts cards show — read
    straight off the published payload rather than recomputed here. `allowed`
    limits which symbols may appear (see _cross_scope); omitted, nothing is
    filtered so the helper stays usable on its own.
    """
    scope = {str(s).upper() for s in (allowed or [])}
    gold, death = [], []
    for a in (alerts or []):
        if not isinstance(a, dict):
            continue
        sym = a.get("symbol")
        if not sym:
            continue
        if scope and str(sym).upper() not in scope:
            continue
        # match the chart's labels: BTC/USD rides the tape as plain BTC
        row = (str(sym).replace("/USD", ""),
               a.get("type") or a.get("detail") or "EMA cross")
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
    tag = ("base confirmed" if bs == "confirmed"
           else f"base forming {rec.get('base_score', '?')}/5" if bs == "forming"
           else "no base — still falling")
    return (f"{pl:.1f}% off low · " if pl is not None else "") + tag


def _reentry_status(rec):
    """Verdict shown as a pill next to the reason (and in the caption).

    Split out of the reason text so the poster can render it as a chip and the
    'is there anything worth waiting on?' count in the standfirst can be taken
    from the same place.
    """
    bs = rec.get("base_status", "")
    return "READY" if bs == "confirmed" else "WAIT" if bs == "forming" else "NOT YET"


# spelled-out counts for the headline — the poster reads as a sentence, and
# "Twelve up, eighteen down." beats "12 up, 18 down." at display size
_ONES = ("zero one two three four five six seven eight nine ten eleven twelve "
         "thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety")


def _words(n):
    """0-999 as words; anything larger falls back to digits."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n < 0 or n > 999:
        return str(n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")
    return _ONES[n // 100] + " hundred" + (" " + _words(n % 100) if n % 100 else "")


def _cap(s):
    return s[:1].upper() + s[1:] if s else s


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
    action, reentry, status = [], [], {}
    for sym in core_syms:
        r = by.get(sym)
        if not ok(r):
            continue
        g = _group(r, positions, hz, lz)
        if g == 1:
            action.append((sym, _action_reason(r, positions.get(sym), hz, gain_pct)))
            status[sym] = "AT HIGH"
        elif g == 3:
            reentry.append((sym, _reentry_reason(r, lz)))
            status[sym] = _reentry_status(r)

    golden, death = _crosses(alerts, _cross_scope(records, core_syms))

    row = lambda r: (r["symbol"], round(r.get("change_pct") or 0, 1), _badge(r))
    dsym = lambda r: r["symbol"].replace("/USD", "")
    r52 = {r["symbol"]: _r52(r) for r in up + down}
    r52.update({dsym(r): _r52(r) for r in idx})
    s = {
        "core_name": core_name, "label": label, "session": session,
        "up": [row(r) for r in up], "down": [row(r) for r in down],
        "idx": [(dsym(r), round(r.get("change_pct") or 0, 1), _badge(r)) for r in idx],
        "action": action, "reentry": reentry, "status": status, "r52": r52,
        "golden": golden, "death": death,
        "up_n": up_n, "down_n": down_n, "watched": len(held),
        "breadth": f"{up_n} up · {down_n} down"
                   + (f" · {ext} at 52-week extremes" if ext else ""),
    }
    s["headline"] = f"{_cap(_words(up_n))} up, {_words(down_n)} down."
    s["standfirst"] = standfirst(s)
    s["edges"] = edges_line(s)
    return s


def standfirst(s):
    """The two-sentence read under the headline — what actually happened.

    Written from the same numbers the poster draws: the day's worst (or, on an
    all-green day, best) Core mover, how many names are sitting near their
    lows, and how many of those have a base underneath them. Crosses get a
    clause only when they printed.
    """
    overnight = (s.get("session") or "") in ("PRE-MARKET", "30 MIN TO OPEN")
    when = "overnight" if overnight else "today"
    movers = (s.get("up") or []) + (s.get("down") or [])
    lead = ""
    if movers:
        # the day's worst Core name leads the read; on an all-green day the best
        # one does instead, so the sentence is never a non-event
        sym, v, _b = min(movers, key=lambda m: m[1])
        if v >= 0:
            sym, v, _b = max(movers, key=lambda m: m[1])
        lead = (f"{sym} gave back {abs(v):.0f}% {when}" if v < 0
                else f"{sym} led the book +{v:.0f}% {when}")

    n = len(s.get("reentry") or [])
    st = s.get("status") or {}
    based = sum(1 for sym, _ in (s.get("reentry") or [])
                if st.get(sym) in ("WAIT", "READY"))
    if n:
        low = (f"{_words(n)} name{'s' if n != 1 else ''} "
               f"{'are' if n != 1 else 'is'} still circling their lows")
        first = f"{lead} and {low}." if lead else f"{_cap(low)}."
        second = ("None of them has a base under it yet." if not based
                  else f"{_cap(_words(based))} of them "
                       f"{'have' if based != 1 else 'has'} a base worth waiting on.")
    else:
        first = f"{lead} and nothing is sitting at its lows." if lead \
            else "Nothing is sitting at its lows."
        second = ""

    g, d = len(s.get("golden") or []), len(s.get("death") or [])
    cross = ""
    if g or d:
        bits = [f"{_words(n)} {name} cross{'es' if n != 1 else ''}"
                for n, name in ((g, "golden"), (d, "death")) if n]
        cross = f"{_cap(' and '.join(bits))} printed."
    return " ".join(x for x in (first, second, cross) if x)


def _and(names):
    """['A'] -> 'A'; ['A','B'] -> 'A and B'; ['A','B','C'] -> 'A, B and C'."""
    if len(names) < 3:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def edges_line(s):
    """Who is at a 52-week extreme, as one sentence.

    The movers are drawn as a plain diverging bar chart — one lane per name,
    nothing else competing for the row — so the 52-week context that used to
    ride under every bar is summarised here instead, under the chart.
    """
    rows = (s.get("up") or []) + (s.get("down") or [])
    lo = [m[0] for m in rows if len(m) > 2 and m[2] == "lo"]
    hi = [m[0] for m in rows if len(m) > 2 and m[2] == "hi"]
    bits = []
    if lo:
        bits.append(f"{_and(lo)} sit{'s' if len(lo) == 1 else ''} near "
                    f"{'its 52-week low' if len(lo) == 1 else '52-week lows'}")
    if hi:
        bits.append(f"{_and(hi)} near {'its high' if len(hi) == 1 else 'their highs'}")
    return " · ".join(bits)


def summary_text(s):
    """Telegram caption + email text fallback.

    Deliberately NOT a transcript of the chart: the movers / index / 52-week
    numbers are already legible in the PNG, so the text carries only what needs
    reading and reasoning about — the grouped action, re-entry and cross
    signals, under one header per group.
    """
    head = (s.get("session") or "").title()
    st = s.get("status") or {}
    lines = [f"TrendAlert Daily · {head}" if head else "TrendAlert Daily",
             f"{s['core_name']} · {s['label']}"]
    if s.get("standfirst"):
        lines += ["", s["standfirst"]]
    if s.get("edges"):
        lines += ["", f"At the edges: {s['edges']}"]
    groups = _callout_groups(s)
    for tag, rows, _col, total in groups:
        lines += ["", f"{tag} ({total})"]
        lines += [f"  {sym:<5} {why}" + (f"  [{st[sym]}]" if st.get(sym) else "")
                  if sym else f"  {why}" for sym, why in rows]
    if not groups:
        lines += ["", "No action, re-entry or cross signals today."]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# render (matplotlib -> PNG bytes)
# ----------------------------------------------------------------------
# The alert is laid out as a poster, not a chart: a headline that reads as a
# sentence, three stat circles, the signal cards, the movers as one diverging
# bar chart, then the index tape with its 52-week rails. Everything is drawn on
# a single axis in LAYOUT UNITS where 1 unit == 1 point == 1 px at 72 dpi, so
# the numbers below are the pixel geometry of the design at its 580-wide
# reference size.
#
# Inks are picked for contrast on the cream page, not for prettiness: every
# text colour here clears WCAG AA (4.5:1) on BG, which is why the accents come
# in two strengths — the saturated one fills bars and dots, the dark one is the
# only one allowed to carry type.
BG = "#F5EEE3"          # cream page
ROW_BG = "#FBF7F0"      # card fill
BAND = "#EAE1D2"        # lane band behind a mover row
BORDER = "#DFD3C0"
INK = "#241F1A"
INK_SOFT = "#4F4740"    # body copy — 8.4:1 on BG
INK_FAINT = "#776E62"   # meta / footnotes — 4.6:1 on BG
RUST = "#8A4E1E"        # eyebrow + section labels — 6.1:1 on BG
GREEN = "#6E8040"       # up (fills)
ORANGE = "#BC5426"      # down (fills)
GREEN_TXT = "#46602A"   # up (type) — 6.0:1 on BG
ORANGE_TXT = "#9E3F16"  # down (type) — 6.3:1 on BG
TRACK = "#E1D7C7"       # 1-day move capsule
RAIL = "#CFC5B4"        # 52-week rail
GREEN_CARD = "#EAF1D8"
AMBER_CARD = "#FBEEDB"
PINK_CARD = "#FBE4DB"
GREEN_TINT = "#DEE9CC"  # best circle
PINK_TINT = "#F8DAD0"   # worst circle
GREY_TINT = "#E4E1D9"   # breadth circle
SESSION_BG = "#A8541F"
SERIF = "DejaVu Serif"  # matplotlib-bundled; no font install on the function
SANS = "DejaVu Sans"
STATUS_BG = {"WAIT": "#5C6640", "NOT YET": "#A03D16",
             "READY": "#3A6335", "AT HIGH": "#8A5A00"}
# one card skin per callout group: (poster title, card fill, label ink)
GROUP_SKIN = {
    "⚡ ACTION": ("ACTION", AMBER_CARD, "#8A5A00"),
    "◎ RE-ENTRY": ("RE-ENTRY WATCH", GREEN_CARD, RUST),
    "▲ GOLDEN CROSS": ("GOLDEN CROSS", GREEN_CARD, "#3F6B3A"),
    "▼ DEATH CROSS": ("DEATH CROSS", PINK_CARD, "#A32B27"),
}

W = 580                 # page width in layout units
PAD = 24
COL = W - 2 * PAD
ROW_H = 74              # an index: ticker + move capsule, then the 52w rail
SIG_H = 53              # a signal row inside a callout card
MOVE_H = 31             # a mover lane in the diverging bar chart
CIRCLES_H = 187


def _spaced(t):
    """Letterspacing, which matplotlib has no property for — hair spaces."""
    return " ".join(t)


def _wrap(text, width):
    import textwrap
    return textwrap.wrap(text, width) or [""]


def _long_date(label):
    """'2026-07-24' -> 'Friday 24 July 2026'; anything else passes through."""
    from datetime import datetime
    try:
        return datetime.strptime(str(label)[:10], "%Y-%m-%d").strftime(
            "%A %d %B %Y").replace(" 0", " ")
    except (ValueError, TypeError):
        return str(label)


class _Sheet:
    """Top-down page painter. ax=None measures the layout without drawing.

    The height of the PNG depends on how many signal rows and movers there are,
    and matplotlib needs the figure size up front — so the same layout code
    runs twice: once dry to total the height, once for real.
    """

    def __init__(self, ax=None):
        self.ax = ax

    def box(self, x, y, w, h, r, fc, ec="none"):
        if self.ax is None:
            return
        from matplotlib.patches import FancyBboxPatch
        self.ax.add_patch(FancyBboxPatch(
            (x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
            fc=fc, ec=ec, lw=0 if ec == "none" else 1.0, zorder=1))

    def txt(self, x, y, t, size, color, family=SANS, weight="normal",
            ha="left", va="center"):
        if self.ax is None:
            return
        self.ax.text(x, y, t, fontsize=size, color=color, family=family,
                     fontweight=weight, ha=ha, va=va, zorder=4)

    def pill(self, x, y, t, size, fc, tc, ha="left"):
        if self.ax is None:
            return
        self.ax.text(x, y, t, fontsize=size, color=tc, family=SANS,
                     fontweight="bold", ha=ha, va="center", zorder=4,
                     bbox=dict(boxstyle="round,pad=0.5,rounding_size=1.4",
                               fc=fc, ec="none"))

    def line(self, x0, x1, y, color, lw=1.0):
        if self.ax is None:
            return
        self.ax.plot([x0, x1], [y, y], color=color, lw=lw,
                     solid_capstyle="round", zorder=2)

    def vline(self, x, y0, y1, color, lw=1.0):
        if self.ax is None:
            return
        self.ax.plot([x, x], [y0, y1], color=color, lw=lw, zorder=3)

    def dot(self, x, y, r, fc, ec):
        if self.ax is None:
            return
        from matplotlib.patches import Circle
        self.ax.add_patch(Circle((x, y), r, fc=fc, ec=ec, lw=1.4, zorder=5))

    def circle(self, cx, cy, r, fc):
        if self.ax is None:
            return
        from matplotlib.patches import Circle
        self.ax.add_patch(Circle((cx, cy), r, fc=fc, ec="none", zorder=2))


def _paint_row(sh, top, m, mx, meta, left, right):
    """One index: ticker, 1-day move capsule, and the 52-week rail beneath it."""
    sym, v = m[0], m[1]
    col = GREEN if v >= 0 else ORANGE
    ymid, yrail = top + 24, top + 52

    sh.txt(left + 14, ymid, sym, 14, INK, SERIF, "bold")

    bx0, bx1 = left + 66, right - 86
    sh.box(bx0, ymid - 6.5, bx1 - bx0, 13, 6.5, TRACK)
    cx, half = (bx0 + bx1) / 2, (bx1 - bx0) / 2 - 4
    span = max(abs(v) / mx * half, 3.5)
    sh.box(cx if v >= 0 else cx - span, ymid - 6.5, span, 13, 6.5, col)
    sh.txt(right - 8, ymid, f"{v:+.1f}%".replace("-", "−"), 12,
           GREEN_TXT if v >= 0 else ORANGE_TXT, SANS, "bold", ha="right")

    mm = meta.get(sym, {})
    lo, hi, c = mm.get("lo"), mm.get("hi"), mm.get("close")
    ph, pl = mm.get("pfh"), mm.get("pfl")
    near_hi = ph is not None and ph >= -2
    near_lo = pl is not None and pl <= 2
    rx0, rx1 = left + 122, right - 86
    sh.line(rx0, rx1, yrail, RAIL, 3.0)
    frac = (c - lo) / (hi - lo) if (hi and lo and hi > lo and c) else 0.5
    sh.dot(rx0 + min(max(frac, 0), 1) * (rx1 - rx0), yrail, 4.2,
           GREEN if near_hi else ORANGE if near_lo else "#3F3A33", BG)
    sh.txt(rx0 - 10, yrail, _money(lo), 10, INK_SOFT, SANS, ha="right")
    sh.txt(rx1 + 10, yrail, _money(hi), 10, INK_SOFT, SANS)
    if near_hi or near_lo:
        sh.txt(left + 2, yrail, _spaced("NEAR HIGH" if near_hi else "NEAR LOW"),
               7.5, GREEN_TXT if near_hi else ORANGE_TXT, SANS, "bold")


def _paint_movers(sh, top, movers, meta, edges):
    """The movers as ONE diverging bar chart. -> the block's height.

    A single lane per name, all bars off a shared zero line whose position
    splits the plot by the day's actual extremes, so one scale serves both
    directions and bar lengths are comparable across the whole chart. A name at
    a 52-week extreme gets a dot past the end of its bar; the sentence under
    the chart says which name that was, which is where the per-row 52-week
    rails went.
    """
    rows = len(movers)
    h = 40 + rows * MOVE_H + (30 if edges else 10)
    sh.box(PAD, top, COL, h, 14, ROW_BG, BORDER)
    sh.txt(PAD + 16, top + 22, _spaced("1-DAY MOVE"), 10, RUST, SANS, "bold")

    bx0, bx1 = PAD + 12, PAD + COL - 60      # lane band
    lx0, lx1 = PAD + 74, bx1 - 18            # plot area (leaves room for the dot)
    up = max((m[1] for m in movers), default=0)
    dn = max((-m[1] for m in movers), default=0)
    scale = (lx1 - lx0) / (max(up, 0) + max(dn, 0) or 1)
    zx = lx0 + max(dn, 0) * scale            # zero line sits where the day put it

    y = top + 40
    # the zero line, drawn once behind the bars: with ten lanes stacked the eye
    # needs something to measure the bars against
    sh.vline(zx, y + 2, y + rows * MOVE_H - 4, "#B9AE9B", 1.2)
    for m in movers:
        v = m[1]
        mid = y + MOVE_H / 2
        sh.box(bx0, y + 2, bx1 - bx0, MOVE_H - 6, 5, BAND)
        sh.txt(PAD + 18, mid, m[0], 12.5, INK, SERIF, "bold")
        span = max(abs(v) * scale, 2.5)
        sh.box(zx if v >= 0 else zx - span, mid - 5.5, span, 11, 2.5,
               GREEN if v >= 0 else ORANGE)
        if len(m) > 2 and m[2]:              # 52-week extreme -> a dot past the bar
            end = (zx + span + 9) if v >= 0 else (zx - span - 9)
            sh.dot(end, mid, 3.2, GREEN if m[2] == "hi" else ORANGE, ROW_BG)
        sh.txt(PAD + COL - 14, mid, f"{v:+.1f}%".replace("-", "−"), 11.5, INK,
               SANS, "bold", ha="right")
        y += MOVE_H

    if edges:
        ey = y + 16
        sh.txt(PAD + 16, ey, _spaced("AT THE EDGES"), 8.5, RUST, SANS, "bold")
        sh.txt(PAD + 108, ey, edges, 10, INK_SOFT)
    return h


def _paint(sh, s):
    """Draw (or measure) the whole poster. -> total page height in units."""
    movers = sorted(s["up"] + s["down"], key=lambda m: m[1], reverse=True)
    idx = sorted(s["idx"], key=lambda m: m[1], reverse=True)
    meta = s.get("r52", {})
    status = s.get("status", {})
    mx = max((abs(m[1]) for m in movers + idx), default=1) or 1

    # ---- masthead ----
    y = 34
    sh.txt(PAD, y, _spaced(f"TRENDALERT · {s['core_name'].upper()}"), 10,
           RUST, SANS, "bold")
    if s.get("session"):
        sh.pill(W - PAD, y, _spaced(s["session"]), 9, SESSION_BG, "#FFF6EA",
                ha="right")
    y += 34

    for line in _wrap(s.get("headline") or "", 26):
        sh.txt(PAD, y + 14, line, 27, INK, SERIF, "bold")
        y += 37
    y += 4
    for line in _wrap(s.get("standfirst") or "", 58):
        sh.txt(PAD, y + 9, line, 12.5, INK_SOFT)
        y += 20
    y += 6
    sh.txt(PAD, y + 8, f"{_long_date(s['label'])} · {s.get('watched', 0)} "
                       f"ticker{'s' if s.get('watched') != 1 else ''} watched",
           10.5, INK_FAINT)
    y += 30

    # ---- three stat circles: best, worst, breadth ----
    best = movers[0] if movers else None
    worst = movers[-1] if movers else None
    for cx, cy, r, fc, val, lab, vc in (
            (PAD + 79, y + 78, 78, GREEN_TINT,
             f"{best[1]:+.1f}%" if best else "—",
             f"{best[0]} · BEST" if best else "BEST", "#3E4A22"),
            (PAD + 243, y + 101, 86, PINK_TINT,
             f"{worst[1]:+.1f}%" if worst else "—",
             f"{worst[0]} · WORST" if worst else "WORST", "#8E2F14"),
            (PAD + 390, y + 71, 57, GREY_TINT,
             f"{s.get('up_n', 0)} / {s.get('down_n', 0)}", "UP / DOWN", INK)):
        sh.circle(cx, cy, r, fc)
        sh.txt(cx, cy - 6, val.replace("-", "−"), 23, vc, SERIF, "bold",
               ha="center")
        sh.txt(cx, cy + 18, _spaced(lab), 7.5, vc, SANS, "bold", ha="center")
    y += CIRCLES_H + 30

    # ---- signal cards (action / re-entry / crosses) ----
    for tag, rows, _col, total in _callout_groups(s):
        title, fill, ink = GROUP_SKIN.get(tag, (tag, GREEN_CARD, RUST))
        sh.txt(PAD, y, _spaced(f"{title} · {total}"), 10, ink, SANS, "bold")
        y += 20
        h = len(rows) * SIG_H + 12
        sh.box(PAD, y, COL, h, 14, fill)
        ry = y + 6
        for i, (sym, why) in enumerate(rows):
            if i:
                sh.line(PAD + 18, PAD + COL - 18, ry, "#FFFFFF", 1.2)
            mid = ry + SIG_H / 2
            sh.txt(PAD + 18, mid, sym, 14, INK, SERIF, "bold")
            sh.txt(PAD + 92, mid, why, 11.5, INK_SOFT)
            if status.get(sym):
                sh.pill(PAD + COL - 18, mid, status[sym], 8.5,
                        STATUS_BG.get(status[sym], RUST), "#FDF8F1", ha="right")
            ry += SIG_H
        y += h + 24

    # ---- movers: one diverging bar chart ----
    if movers:
        y += _paint_movers(sh, y, movers, meta, s.get("edges") or "") + 22

    # ---- indexes, unchanged: capsule over a 52-week rail ----
    if idx:
        h = 34 + len(idx) * ROW_H + 10
        sh.box(PAD, y, COL, h, 14, GREEN_CARD)
        sh.txt(PAD + 18, y + 20, _spaced("INDEXES"), 10, RUST, SANS, "bold")
        ry = y + 34
        for m in idx:
            _paint_row(sh, ry, m, mx, meta, PAD + 16, W - PAD - 16)
            ry += ROW_H
        y += h + 20

    sh.txt(PAD, y + 8, s.get("breadth", ""), 10, INK_SOFT)
    y += 22
    sh.txt(PAD, y + 8, "Index rows: the dot marks the last price inside the "
                       "52-week range. Not advice.", 10, INK_FAINT)
    return y + 30


def render_chart_png(s):
    """The daily summary as a single-column poster PNG.

    Masthead and session badge, a spelled-out headline over the standfirst,
    three stat circles (best / worst / breadth), one card per signal group with
    a verdict chip on each row, then every mover and index as a 1-day move
    capsule above its 52-week rail — the dot glows green near the 52w high and
    orange near the low. Height is computed from the content by painting the
    layout once with a null canvas.
    """
    import matplotlib
    matplotlib.use("Agg")                   # headless; imported lazily so the pure
    import matplotlib.pyplot as plt          # logic (and tests) need no matplotlib

    height = _paint(_Sheet(None), s)
    fig = plt.figure(figsize=(W / 72, height / 72), dpi=150, facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(height, 0)                  # inverted: lay the page out top-down
    ax.set_facecolor(BG)
    ax.axis("off")
    _paint(_Sheet(ax), s)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
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


def run_daily_summary(bucket, force=False):
    """Never raises. -> dict for the HTTP response.

    force=True (?mode=summary&force=1) bypasses the trading-day gate so the
    alert can be fired by hand outside market hours to eyeball the real
    Telegram/email output. Schedulers never pass it.
    """
    try:
        et = _eastern_now()
        if not is_trading_day(et.date()) and not force:
            return {"ok": True, "summary": "skipped(non-trading-day)"}

        # the same code serves both scheduled runs, so which switch applies is
        # decided by the clock — Settings > Alerts lists them separately
        session = session_label(et)
        kind = "summary_premkt" if session in ("PRE-MARKET", "30 MIN TO OPEN") \
            else "summary_close"
        cfg = _read_json(bucket, "settings.json", {}) or {}
        if not alert_on(cfg, kind):
            return {"ok": True, "summary": f"skipped(off:{kind})"}

        data = _read_json(bucket, "data.json", {}) or {}
        records = data.get("symbols", [])
        if not records:
            return {"ok": False, "summary": "error(no-data.json)"}

        raw = data.get("expected_last_trading_day") \
            or (str(data.get("generated_at", ""))[:10] or None)
        label = raw or et.strftime("%b %d")

        positions = _read_json(bucket, "positions.json", {}) or {}
        if isinstance(positions, dict) and "positions" in positions:
            positions = positions["positions"]
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

        status = fan_out(cfg, (
            ("telegram", lambda: send_telegram_photo(png, caption)),
            ("email", lambda: send_email_image(subject, caption, png)),
        ))
        return {"ok": True, "summary": status,
                "movers": len(s["up"]) + len(s["down"])}
    except Exception as e:
        return {"ok": False, "summary": f"error({type(e).__name__})"}
