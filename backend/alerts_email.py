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
  TELEGRAM_CHAT_ID    chat to notify (from getUpdates after messaging the bot)

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
def _send(lines, asof):
    user = os.environ.get("SMTP_USER", "")
    if not user:
        return "email:disabled"
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    pw = os.environ.get("SMTP_PASS", "")
    to = [a.strip() for a in os.environ.get("ALERT_TO", user).split(",") if a.strip()]

    body = (f"TrendAlert EOD — {asof}\n\n" + "\n\n".join(lines) +
            "\n\nSignals are computed on confirmed daily closes. "
            "Execution reference: next session's open.\n")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"TrendAlert EOD {asof}: {len(lines)} signal{'s' if len(lines) != 1 else ''}"
    msg["From"] = os.environ.get("ALERT_FROM", user)
    msg["To"] = ", ".join(to)

    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(msg["From"], to, msg.as_string())
    return f"email:sent({len(lines)})"


def _send_telegram(lines, asof):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return "telegram:disabled"
    import requests
    body = (f"TrendAlert EOD — {asof}\n\n" + "\n\n".join(lines) +
            "\n\nSignals are computed on confirmed daily closes. "
            "Execution reference: next session's open.")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": body}, timeout=20)
    r.raise_for_status()
    return f"telegram:sent({len(lines)})"


def send_eod_alerts(records, cross_alerts, bucket, asof, trading_day=True):
    """Main hook called from the pipeline. Never raises."""
    try:
        if not trading_day:
            return "email:skipped(non-trading-day)"
        positions = _read_json(bucket, "positions.json", {}) or {}
        if isinstance(positions, dict) and "positions" in positions:
            positions = positions["positions"]
        settings = _read_json(bucket, "settings.json", {}) or {}
        state = _read_json(bucket, STATE_OBJECT, {}) or {}

        events = detect_events(records, positions, settings)
        lines, new_state = transition_filter(events, cross_alerts, state)
        _write_json(bucket, STATE_OBJECT, new_state)
        if not lines:
            return "alerts:none"
        # each channel is independent — one failing must not block the other
        statuses = []
        for sender, tag in ((_send_telegram, "telegram"), (_send, "email")):
            try:
                statuses.append(sender(lines, asof))
            except Exception as e:
                statuses.append(f"{tag}:error({type(e).__name__})")
        return "+".join(statuses)
    except Exception as e:
        return f"alerts:error({type(e).__name__})"
