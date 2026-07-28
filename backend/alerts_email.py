"""
alerts_email.py — end-of-day alert email for the TrendAlert pipeline.

Fires ONE summary email per pipeline run when a rule-engine event happens:
  BOOK ⅓     position (not yet booked) gained >= gainPct AND is inside the
             near-high zone — both Option C booking conditions met on the close
  TRAIL EXIT booked position closed below EMA50
  BASE ✓     base_status flipped to "confirmed" (re-entry candidate)
  CROSS      golden / death EMA50-150 crosses (passed in from the pipeline)

De-duplication: alerts_state.json in the bucket stores the last event emitted
per symbol; an event is only mailed on TRANSITION (e.g. BASE ✓ mails once,
not every day the base stays confirmed).

Config (env — leave SMTP_USER unset to disable email entirely):
  SMTP_HOST  default smtp.gmail.com     SMTP_PORT  default 587 (STARTTLS)
  SMTP_USER  sender account             SMTP_PASS  app password (Secret Manager)
  ALERT_TO   comma-separated recipients (default: SMTP_USER)

Telegram (leave either unset to disable; both channels can run side by side):
  TELEGRAM_BOT_TOKEN  BotFather token (Secret Manager: telegram-bot-token)
  TELEGRAM_CHAT_ID    comma-separated chat ids to notify (one bot, many
                       people); each person must message the bot once so
                       their id can be read from getUpdates — bots can't
                       message first

Positions come from positions.json (written by the notes function when the
dashboard syncs); thresholds come from published settings.json (defaults 20/2).
"""

import json
import os
import smtplib
from email.mime.text import MIMEText

STATE_OBJECT = "alerts_state.json"
DEFAULT_GAIN_PCT = 20.0
DEFAULT_HIGH_ZONE_PCT = 2.0
CHANNELS = ("telegram", "email", "both")
DEFAULT_CHANNEL = "both"

# Every alert this project sends, in the order the trading day fires them.
# (key, dashboard label, when it runs) — the dashboard renders this list as the
# Settings > Alerts switches, and each entry point checks its own key, so an
# alert that is switched off costs nothing but the settings read.
ALERT_KINDS = (
    ("summary_premkt", "Pre-market summary", "8:35 ET · the chart"),
    ("brief", "Morning brief", "9:50 ET"),
    ("summary_close", "Market-close summary", "16:50 ET · the chart"),
)
ALERT_KEYS = tuple(k for k, _l, _w in ALERT_KINDS)
# Retired at the user's request (2026-07-28): no switch, and never sent again.
# The code and the ?mode=bloodbath route are kept so this is a one-line undo,
# but alert_on says no whatever settings.json holds — a retired alert must not
# come back to life through the fail-open default the live ones rely on.
RETIRED_ALERTS = ("bloodbath", "eod")


# ----------------------------------------------------------------------
# delivery channel (Settings > Alerts -> settings.json "alertChannel")
# ----------------------------------------------------------------------
def alert_channel(settings):
    """-> 'telegram' | 'email' | 'both'. Anything unrecognised means both.

    A missing or junk value must never silence the alerts, so the default is
    the most-delivered option rather than the least.
    """
    v = (settings or {}).get("alertChannel") if isinstance(settings, dict) else None
    return v if v in CHANNELS else DEFAULT_CHANNEL


def alert_on(settings, kind):
    """Is this alert switched on? Missing / junk means on, like the channel.

    Silence has to be chosen explicitly: a settings.json that predates the
    switches, or one written by a browser that does not know this alert yet,
    must keep delivering it. Retired alerts are the one exception — they are
    off for good, and no stored setting can turn them back on.
    """
    if kind in RETIRED_ALERTS:
        return False
    types = (settings or {}).get("alertTypes") if isinstance(settings, dict) else None
    if not isinstance(types, dict):
        return True
    return types.get(kind, True) is not False


def channel_on(settings, ch):
    """Is this channel switched on for the user? (env config still gates it.)"""
    picked = alert_channel(settings)
    return picked == "both" or picked == ch


def fan_out(settings, senders):
    """Run each ('telegram'|'email', callable) the user still wants, safely.

    Every alert in the pipeline delivers the same way: both channels are
    independent, one failing or being switched off must not stop the other,
    and the joined statuses become the HTTP response body.
    """
    statuses = []
    for ch, send in senders:
        if not channel_on(settings, ch):
            statuses.append(f"{ch}:off")
            continue
        try:
            statuses.append(send())
        except Exception as e:
            statuses.append(f"{ch}:error({type(e).__name__})")
    return "+".join(statuses)


# ----------------------------------------------------------------------
# GCS helpers
# ----------------------------------------------------------------------
def _read_json(bucket, name, default):
    try:
        from google.cloud import storage
        blob = storage.Client().bucket(bucket).blob(name)
        if not blob.exists():
            return default
        return json.loads(blob.download_as_text())
    except Exception:
        return default


def _write_json(bucket, name, obj):
    try:
        from google.cloud import storage
        blob = storage.Client().bucket(bucket).blob(name)
        blob.cache_control = "no-cache, max-age=0"
        blob.upload_from_string(json.dumps(obj), content_type="application/json")
    except Exception:
        pass  # state write failure must never break the pipeline


# ----------------------------------------------------------------------
# event detection (mirrors the dashboard's Option C rule engine)
# ----------------------------------------------------------------------
def detect_events(records, positions, settings):
    """-> list of {symbol, event, line} — the day's rule-engine signals."""
    gain_pct = float(settings.get("gainPct", DEFAULT_GAIN_PCT))
    high_zone = float(settings.get("highZonePct", DEFAULT_HIGH_ZONE_PCT))
    events = []
    for rec in records:
        sym = rec.get("symbol")
        close = rec.get("close")
        if sym is None or close is None:
            continue
        pos = positions.get(sym)

        if pos and pos.get("entry"):
            entry = float(pos["entry"])
            gain = (close - entry) / entry * 100.0
            pct_hi = rec.get("pct_from_high")
            near_high = pct_hi is not None and pct_hi >= -high_zone
            if not pos.get("booked") and gain >= gain_pct and near_high:
                events.append({
                    "symbol": sym, "event": "book",
                    "line": (f"BOOK 1/3 — {sym} closed ${close:.2f}, "
                             f"+{gain:.1f}% since entry ${entry:.2f} and "
                             f"{abs(pct_hi):.1f}% from the 52w high (zone {high_zone}%).")})
            elif pos.get("booked") and rec.get("ema50") is not None and close < rec["ema50"]:
                events.append({
                    "symbol": sym, "event": "trail_exit",
                    "line": (f"TRAIL EXIT — {sym} closed ${close:.2f} below "
                             f"EMA50 ${rec['ema50']:.2f}; review the remaining 2/3 "
                             f"(entry ${entry:.2f}, {gain:+.1f}%).")})

        if rec.get("base_status") == "confirmed" and not (pos and pos.get("entry")):
            score = rec.get("base_score")
            events.append({
                "symbol": sym, "event": "base_confirmed",
                "line": (f"BASE CONFIRMED — {sym} at ${close:.2f}"
                         f"{f' ({score}/5 checks)' if score is not None else ''}; "
                         f"re-entry candidate per the base-formation rule.")})
    return events


def transition_filter(events, cross_alerts, state):
    """Keep only events that CHANGED since the last email; crosses always pass."""
    fresh, new_state = [], {}
    for ev in events:
        new_state[ev["symbol"]] = ev["event"]
        if state.get(ev["symbol"]) != ev["event"]:
            fresh.append(ev["line"])
    for al in cross_alerts:  # single-day events by construction
        fresh.append(f"{al.get('detail', 'Cross')} — {al['symbol']}: {al['type']}.")
    return fresh, new_state


# ----------------------------------------------------------------------
# send
# ----------------------------------------------------------------------
def send_email_text(subject, body):
    """Generic email sender (EOD alerts + morning brief share the config)."""
    user = os.environ.get("SMTP_USER", "")
    if not user:
        return "email:disabled"
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    pw = os.environ.get("SMTP_PASS", "")
    to = [a.strip() for a in os.environ.get("ALERT_TO", user).split(",") if a.strip()]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = os.environ.get("ALERT_FROM", user)
    msg["To"] = ", ".join(to)

    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(msg["From"], to, msg.as_string())
    return "email:sent"


def send_telegram_text(body):
    """Generic Telegram sender — one bot, comma-separated chat ids; each
    person must have messaged the bot once (bots cannot message first)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chat_ids:
        return "telegram:disabled"
    import requests
    sent, failed = 0, 0
    for chat_id in chat_ids:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat_id, "text": body}, timeout=20)
            r.raise_for_status()
            sent += 1
        except Exception:
            failed += 1   # one recipient's bad/blocked chat must not stop the rest
    return f"telegram:sent({sent}/{len(chat_ids)})" if not failed else f"telegram:sent({sent}/{len(chat_ids)},failed={failed})"


def send_telegram_photo(png_bytes, caption=""):
    """Send a PNG as a Telegram photo (caption capped at Telegram's 1024)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chat_ids:
        return "telegram:disabled"
    import requests
    sent, failed = 0, 0
    for chat_id in chat_ids:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                              data={"chat_id": chat_id, "caption": caption[:1024]},
                              files={"photo": ("summary.png", png_bytes, "image/png")},
                              timeout=30)
            r.raise_for_status()
            sent += 1
        except Exception:
            failed += 1
    return f"tg-photo:sent({sent}/{len(chat_ids)})" if not failed else f"tg-photo:sent({sent}/{len(chat_ids)},failed={failed})"


def send_email_image(subject, body_text, png_bytes, filename="summary.png"):
    """Email with the chart inlined via cid + a plain-text alternative."""
    user = os.environ.get("SMTP_USER", "")
    if not user:
        return "email:disabled"
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    pw = os.environ.get("SMTP_PASS", "")
    to = [a.strip() for a in os.environ.get("ALERT_TO", user).split(",") if a.strip()]

    from email.mime.multipart import MIMEMultipart
    from email.mime.image import MIMEImage
    from html import escape

    root = MIMEMultipart("related")
    root["Subject"] = subject
    root["From"] = os.environ.get("ALERT_FROM", user)
    root["To"] = ", ".join(to)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body_text, "plain", "utf-8"))
    html = ('<div style="font-family:system-ui,-apple-system,sans-serif;max-width:640px">'
            '<img src="cid:chart" alt="TrendAlert daily summary" '
            'style="width:100%;border-radius:10px;border:1px solid #eee">'
            '<pre style="font-size:12px;line-height:1.5;color:#666;white-space:pre-wrap">'
            + escape(body_text) + '</pre></div>')
    alt.attach(MIMEText(html, "html", "utf-8"))
    root.attach(alt)
    img = MIMEImage(png_bytes, "png")
    img.add_header("Content-ID", "<chart>")
    img.add_header("Content-Disposition", "inline", filename=filename)
    root.attach(img)

    with smtplib.SMTP(host, port, timeout=30) as sm:
        sm.starttls()
        sm.login(user, pw)
        sm.sendmail(root["From"], to, root.as_string())
    return "email:sent"


def _eod_body(lines, asof):
    return (f"TrendAlert EOD — {asof}\n\n" + "\n\n".join(lines) +
            "\n\nSignals are computed on confirmed daily closes. "
            "Execution reference: next session's open.\n")


def _send(lines, asof):
    subject = f"TrendAlert EOD {asof}: {len(lines)} signal{'s' if len(lines) != 1 else ''}"
    status = send_email_text(subject, _eod_body(lines, asof))
    return f"email:sent({len(lines)})" if status == "email:sent" else status


def _send_telegram(lines, asof):
    return send_telegram_text(_eod_body(lines, asof).rstrip("\n"))


def send_eod_alerts(records, cross_alerts, bucket, asof, trading_day=True):
    """Main hook called from the pipeline. Never raises."""
    try:
        if not trading_day:
            return "email:skipped(non-trading-day)"
        positions = _read_json(bucket, "positions.json", {}) or {}
        if isinstance(positions, dict) and "positions" in positions:
            positions = positions["positions"]
        settings = _read_json(bucket, "settings.json", {}) or {}
        if not alert_on(settings, "eod"):
            return "alerts:retired"
        state = _read_json(bucket, STATE_OBJECT, {}) or {}

        events = detect_events(records, positions, settings)
        lines, new_state = transition_filter(events, cross_alerts, state)
        _write_json(bucket, STATE_OBJECT, new_state)
        if not lines:
            return "alerts:none"
        return fan_out(settings, (("telegram", lambda: _send_telegram(lines, asof)),
                                  ("email", lambda: _send(lines, asof))))
    except Exception as e:
        return f"alerts:error({type(e).__name__})"
