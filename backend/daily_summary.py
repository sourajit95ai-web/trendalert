"""
daily_summary.py — Core-portfolio EOD summary pushed as a CHART IMAGE.

Triggered by Cloud Scheduler hitting the pipeline function with ?mode=summary
(pre-market ~08:35 ET and after the close ~16:50 ET). It does NOT recompute or
overwrite data.json — it reads the published close and paints the same picture
as the dashboard's daily-summary card, then delivers it as a PNG:

  MOVERS   top 3 up / 3 down among the CORE portfolio holdings
  INDEX    QQQ / SPY / BTC pinned, plus the best + worst of the rest
  BREADTH  up / down count + 52-week extremes across the holdings

Core membership comes from core.json (published by the dashboard, union-merged
server-side so a stale browser can't shrink it); falls back to CORE_SYMBOLS.
Delivery reuses the EOD channels: a Telegram photo + an email with the chart
inlined (alerts_email.send_telegram_photo / send_email_image). Never raises.
"""

import io

from alerts_email import (_read_json, alert_on, caption_text_on, fan_out,
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
TOP_N = 3                                     # movers shown per side: TOP 3 · WORST 3
# the whole caption — the poster carries the reading, this carries the way in
DASHBOARD_URL = "https://storage.googleapis.com/trendalert-data-rattle/next/dashboard.html"


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
    up = eq[:TOP_N]
    up_set = {r["symbol"] for r in up}
    down = [r for r in reversed(eq) if r["symbol"] not in up_set][:TOP_N]

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
    # the market's own read, next to the book's: QQQ rides the poster's stat
    # circles as NASDAQ, not just as another row in the index panel
    s["nasdaq"] = next((m for m in s["idx"] if m[0] == "QQQ"), None)
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


def signal_text(s):
    """The written read: the grouped action, re-entry and cross signals.

    Deliberately NOT a transcript of the poster: the movers / index / 52-week
    numbers are already legible in the PNG, so this carries only what needs
    reading and reasoning about, under one header per group. Off by default —
    see Settings > Alerts, and summary_text below.
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


def summary_text(s=None, verbose=False):
    """Telegram caption + email text: the dashboard link, and by default only that.

    Everything worth reading — headline, standfirst, the signal cards and both
    tapes — is already in the poster, and the text under it was a second copy
    of the same thing. `verbose` (Settings > Alerts -> settings.json
    "captionText") puts the written signals back above the link for anyone who
    wants to read rather than look.
    """
    if verbose and s:
        return f"{signal_text(s)}\n\n{DASHBOARD_URL}"
    return DASHBOARD_URL


# ----------------------------------------------------------------------
# render (matplotlib -> PNG bytes)
# ----------------------------------------------------------------------
# The alert is laid out as a poster, not a chart: a headline that reads as a
# sentence, four stat circles, the signal sections, then the movers and the
# indexes as one-line rows carrying the move and a 52-week range. Everything
# is drawn on a single axis in LAYOUT UNITS where 1 unit == 1 point == 1 px at 72 dpi, so
# the numbers below are the pixel geometry of the design at its 580-wide
# reference size.
#
# The page is dark, and the inks are picked for contrast on it rather than for
# prettiness: every text colour here clears WCAG AA (4.5:1) on BG. Note the
# direction flip from the light edition — on this ground the light tints are
# fills that carry DARK type, and the saturated accents are what carry type on
# the page itself.
BG = "#2E2B24"          # warm near-black page
CREAM = "#F2EBDD"       # headline, tickers, the loudest type — 11.5:1 on BG
BODY = "#C9C1B2"        # standfirst and row reasons — 8.0:1 on BG
FAINT = "#A09786"       # date, footnote — 4.9:1 on BG
ORANGE = "#E8834F"      # section labels, the headline's closing line, down type
GREEN_TXT = "#A9C48A"   # up type — 7.4:1 on BG
RULE = "#46423A"        # row separators
RAIL = "#514C43"        # the 52-week track
# stat-circle fills are light discs on the dark page, so their type flips to ink
GREEN_FILL, GREEN_INK = "#D9E7C4", "#33421C"
PINK_FILL, PINK_INK = "#FAE0D2", "#8E2F14"
GREY_FILL, GREY_INK = "#E6E2DA", "#2B2721"
SERIF = "DejaVu Serif"  # matplotlib-bundled; no font install on the function
SANS = "DejaVu Sans"
# the verdict at the end of a signal row — plain caps, no chip
STATUS_INK = {"WAIT": CREAM, "NOT YET": ORANGE,
              "READY": GREEN_TXT, "AT HIGH": "#E8B45F"}
# a callout group's poster title. The design only shows RE-ENTRY WATCH, because
# that is what the day had; the other three keep the same row grammar.
GROUP_TITLE = {
    "⚡ ACTION": "ACTION",
    "◎ RE-ENTRY": "RE-ENTRY WATCH",
    "▲ GOLDEN CROSS": "GOLDEN CROSS",
    "▼ DEATH CROSS": "DEATH CROSS",
}
# the masthead names the run rather than badging it. session_label's value is
# still what the caption and the email subject carry.
EDITION = {
    "PRE-MARKET": "PRE-MARKET EDITION",
    "30 MIN TO OPEN": "OPENING BELL EDITION",
    "MORNING SESSION": "MORNING EDITION",
    "MIDDAY": "MIDDAY EDITION",
    "POWER HOUR": "POWER HOUR EDITION",
    "MARKET CLOSE": "EVENING EDITION",
}

W = 580                 # page width in layout units
PAD = 32
COL = W - 2 * PAD
TAPE_H = 40             # one name: ticker + move + 52-week range
SIG_H = 40              # a signal row (ticker + reason + verdict)
CIRCLES_H = 172


def _spaced(t):
    """Letterspacing, which matplotlib has no property for — hair spaces."""
    return " ".join(t)


def _wrap(text, width):
    import textwrap
    return textwrap.wrap(text, width) or [""]


def _short_date(label):
    """'2026-07-24' -> 'Fri 24 Jul'; anything else passes through.

    Short because it sits opposite the masthead on one line, where the old
    long form competed with the eyebrow for the same strip of page.
    """
    from datetime import datetime
    try:
        return datetime.strptime(str(label)[:10], "%Y-%m-%d").strftime(
            "%a %d %b").replace(" 0", " ")
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

    def txt(self, x, y, t, size, color, family=SANS, weight="normal",
            ha="left", va="center"):
        if self.ax is None:
            return
        self.ax.text(x, y, t, fontsize=size, color=color, family=family,
                     fontweight=weight, ha=ha, va=va, zorder=4)

    def line(self, x0, x1, y, color, lw=1.0, dotted=False):
        if self.ax is None:
            return
        self.ax.plot([x0, x1], [y, y], color=color, lw=lw,
                     linestyle=(0, (1, 3)) if dotted else "-",
                     solid_capstyle="round", zorder=2)

    def dot(self, x, y, r, fc):
        if self.ax is None:
            return
        from matplotlib.patches import Circle
        self.ax.add_patch(Circle((x, y), r, fc=fc, ec=BG, lw=1.4, zorder=5))

    def circle(self, cx, cy, r, fc, ec="none"):
        """Filled disc, or — with fc='none' — the outlined ring the design
        gives the benchmark, so the tape reads as context and not as a verdict
        the book earned."""
        if self.ax is None:
            return
        from matplotlib.patches import Circle
        self.ax.add_patch(Circle((cx, cy), r, fc=fc, ec=ec,
                                 lw=0 if ec == "none" else 1.8, zorder=2))


def _paint_tape(sh, top, rows, meta, dotted=True):
    """One line per name: ticker · today's move · where the year leaves it.

    The diverging capsule the previous poster drew is gone — the move is now
    read as type, and the only drawn thing in the row is the 52-week track:
    low, rail, a dot at the last close, high.

    The dot takes the colour of the DAY's move, following the design. That is a
    deliberate trade: the old poster coloured it by 52-week proximity, so a
    name at its high showed green even on a red day. The extreme still gets
    said in words — edges_line carries it into the caption.
    -> the block's height.
    """
    px = PAD + 72                                 # % value
    lx = PAD + 183                                # 52w low, right-aligned
    rx0, rx1 = PAD + 192, W - PAD - 61            # the rail
    hx = W - PAD - 50                             # 52w high

    y = top
    for i, m in enumerate(rows):
        sym, v = m[0], m[1]
        mid = y + TAPE_H / 2
        if i:
            sh.line(PAD, W - PAD, y, RULE, 0.9, dotted=dotted)
        sh.txt(PAD, mid, sym, 13, CREAM, SERIF, "bold")
        sh.txt(px, mid, f"{v:+.1f}%".replace("-", "−"), 11.5,
               GREEN_TXT if v >= 0 else ORANGE, SANS, "bold")

        mm = meta.get(sym, {})
        lo, hi, c = mm.get("lo"), mm.get("hi"), mm.get("close")
        sh.txt(lx, mid, _money(lo), 9.5, BODY, SANS, ha="right")
        sh.line(rx0, rx1, mid, RAIL, 3.4)
        frac = (c - lo) / (hi - lo) if (hi and lo and hi > lo and c) else 0.5
        sh.dot(rx0 + min(max(frac, 0), 1) * (rx1 - rx0), mid, 4.6,
               GREEN_TXT if v >= 0 else ORANGE)
        sh.txt(hx, mid, _money(hi), 9.5, BODY, SANS)
        y += TAPE_H
    return y - top


def _paint(sh, s):
    """Draw (or measure) the whole poster. -> total page height in units."""
    movers = sorted(s["up"] + s["down"], key=lambda m: m[1], reverse=True)
    idx = sorted(s["idx"], key=lambda m: m[1], reverse=True)
    meta = s.get("r52", {})
    status = s.get("status", {})

    # ---- masthead ----
    y = 34
    edition = EDITION.get(s.get("session") or "", s.get("session") or "")
    sh.txt(PAD, y, _spaced("TRENDALERT" + (f" · {edition}" if edition else "")),
           9.5, ORANGE, SANS, "bold")
    sh.txt(W - PAD, y, _short_date(s["label"]), 9.5, FAINT, SANS, ha="right")
    y += 40

    # The headline is a sentence and it breaks at its own comma, not at a
    # measured width: "Twelve up," in cream sits over "eighteen down." in
    # orange, so the accent always lands on the whole closing clause. Wrapping
    # by width instead put the break mid-clause on longer counts
    # ("Twenty-one up, ten" / "down.").
    head, sep, tail = (s.get("headline") or "").partition(", ")
    lines = ([(l, False) for l in _wrap(head + ",", 18)]
             + [(l, True) for l in _wrap(tail, 18)]) if sep \
        else [(l, False) for l in _wrap(head, 18)]
    for line, accent in lines:
        sh.txt(PAD, y + 15, line, 30, ORANGE if accent else CREAM, SERIF, "bold")
        y += 42
    y += 6
    for line in _wrap(s.get("standfirst") or "", 58):
        sh.txt(PAD, y + 9, line, 12.5, BODY)
        y += 21
    y += 20

    # ---- four stat circles: best, worst, breadth, Nasdaq ----
    # the book's own best and worst as filled discs, the count as a neutral
    # one, and the tape it is being judged against as an outlined ring — the
    # benchmark is context, not something the book did.
    best = movers[0] if movers else None
    worst = movers[-1] if movers else None
    nas = s.get("nasdaq")
    nas_ink = GREY_INK if not nas else GREEN_TXT if nas[1] >= 0 else ORANGE
    for cx, cy, r, fc, ec, val, lab, vc in (
            (PAD + 70, y + 74, 67, GREEN_FILL, "none",
             f"{best[1]:+.1f}%" if best else "—",
             f"{best[0]} · BEST" if best else "BEST", GREEN_INK),
            (PAD + 219, y + 88, 76, PINK_FILL, "none",
             f"{worst[1]:+.1f}%" if worst else "—",
             f"{worst[0]} · WORST" if worst else "WORST", PINK_INK),
            (PAD + 353, y + 70, 55, GREY_FILL, "none",
             f"{s.get('up_n', 0)} / {s.get('down_n', 0)}", "UP / DOWN", GREY_INK),
            (PAD + 467, y + 90, 41, "none", nas_ink,
             f"{nas[1]:+.1f}%" if nas else "—", "NASDAQ", nas_ink)):
        sh.circle(cx, cy, r, fc, ec)
        size = 22 if r >= 67 else 17 if r >= 55 else 14
        sh.txt(cx, cy - (6 if r >= 55 else 4), val.replace("-", "−"),
               size, vc, SERIF, "bold", ha="center")
        sh.txt(cx, cy + (18 if r >= 55 else 13), _spaced(lab), 7, vc, SANS,
               "bold", ha="center")
    y += CIRCLES_H + 26

    # ---- signal sections (action / re-entry / crosses) ----
    # No cards any more: a label, then rows, then air. The verdict rides at the
    # end of its own row as plain caps.
    for tag, rows, _col, total in _callout_groups(s):
        title = GROUP_TITLE.get(tag, tag)
        # the count is only worth saying when the panel is showing fewer names
        # than there are, which is exactly when a '+N more' row was added
        capped = total > MAX_ROWS
        sh.txt(PAD, y, _spaced(title + (f" · {total}" if capped else "")),
               9.5, ORANGE, SANS, "bold")
        y += 24
        for i, (sym, why) in enumerate(rows):
            if i:
                sh.line(PAD, W - PAD, y, RULE, 0.9)
            mid = y + SIG_H / 2
            sh.txt(PAD, mid, sym, 13, CREAM, SERIF, "bold")
            sh.txt(PAD + 86, mid, why, 11.5, BODY)
            if status.get(sym):
                sh.txt(W - PAD, mid, _spaced(status[sym]), 9.5,
                       STATUS_INK.get(status[sym], ORANGE), SANS, "bold",
                       ha="right")
            y += SIG_H
        y += 26

    # ---- movers, then the indexes below them ----
    if movers:
        sh.txt(PAD, y, _spaced(f"TOP {TOP_N} · WORST {TOP_N}"), 9.5, ORANGE,
               SANS, "bold")
        sh.txt(W - PAD, y, _spaced("52-WEEK RANGE"), 9.5, ORANGE, SANS, "bold",
               ha="right")
        y += 22
        y += _paint_tape(sh, y, movers, meta) + 26

    if idx:
        sh.txt(PAD, y, _spaced("INDEXES"), 9.5, FAINT, SANS, "bold")
        y += 22
        # solid separators here, dotted above: the indexes are one continuous
        # block, the movers are two halves of a list
        y += _paint_tape(sh, y, idx, meta, dotted=False) + 18

    sh.line(PAD, W - PAD, y, RULE, 0.9)
    sh.txt(PAD, y + 20, "Dot marks last price in the 52-week range · Not advice",
           9.5, FAINT)
    return y + 42


def render_chart_png(s):
    """The daily summary as a single-column poster PNG, dark edition.

    Masthead naming the run and the date, a spelled-out headline whose closing
    line takes the accent, the standfirst, four stat circles (best / worst /
    breadth as discs, the Nasdaq as a ring), then a section per signal group
    with the verdict at the end of each row, then TOP N · WORST N and the
    indexes as one-line rows carrying the move and the 52-week range. Height is
    computed from the content by painting the layout once with a null canvas.
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
        caption = summary_text(s, caption_text_on(cfg))
        subject = f"TrendAlert Daily {raw or ''} — {session.title()}".strip()

        status = fan_out(cfg, (
            ("telegram", lambda: send_telegram_photo(png, caption)),
            ("email", lambda: send_email_image(subject, caption, png,
                                               settings=cfg)),
        ))
        return {"ok": True, "summary": status,
                "movers": len(s["up"]) + len(s["down"])}
    except Exception as e:
        return {"ok": False, "summary": f"error({type(e).__name__})"}
