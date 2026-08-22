"""Alert delivery channel — Settings > Alerts (settings.json "alertChannel").

The switch lives server-side so it applies to every SCHEDULED alert regardless
of which browser last saved settings. These tests pin the two things that
matter: an unset/garbled value must never silence the alerts, and switching a
channel off must not stop the other one from sending.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alerts_email import (ALERT_KEYS, ALERT_KINDS,
                          RETIRED_ALERTS, alert_channel, alert_on,
                          caption_text_on, channel_on,
                          email_recipients, fan_out)


def test_default_is_both_for_missing_or_junk():
    for cfg in ({}, None, {"alertChannel": ""}, {"alertChannel": "carrier-pigeon"},
                {"alertChannel": None}, "not-a-dict"):
        assert alert_channel(cfg) == "both"
        assert channel_on(cfg, "telegram") and channel_on(cfg, "email")


def test_single_channel_selection():
    tg = {"alertChannel": "telegram"}
    assert channel_on(tg, "telegram") and not channel_on(tg, "email")
    em = {"alertChannel": "email"}
    assert channel_on(em, "email") and not channel_on(em, "telegram")


def test_fan_out_skips_the_disabled_channel():
    calls = []
    senders = (("telegram", lambda: calls.append("tg") or "tg-photo:sent(1/1)"),
               ("email", lambda: calls.append("em") or "email:sent"))
    assert fan_out({"alertChannel": "telegram"}, senders) == "tg-photo:sent(1/1)+email:off"
    assert calls == ["tg"]                      # the email sender never ran

    calls.clear()
    assert fan_out({"alertChannel": "email"}, senders) == "telegram:off+email:sent"
    assert calls == ["em"]

    calls.clear()
    assert fan_out({}, senders) == "tg-photo:sent(1/1)+email:sent"
    assert calls == ["tg", "em"]


def test_every_scheduled_alert_has_a_switch():
    """The alerts the schedulers still fire, each with its own key."""
    assert ALERT_KEYS == ("summary_premkt", "brief", "summary_close")
    assert all(len(k) == 3 and all(k) for k in ALERT_KINDS)   # key, label, when
    # a retired alert is not offered as a switch and cannot be one
    assert not set(RETIRED_ALERTS) & set(ALERT_KEYS)


def test_retired_alerts_stay_off_whatever_settings_say():
    """Fail-open is for live alerts only — a retired one must not come back."""
    for kind in RETIRED_ALERTS:
        assert not alert_on({}, kind)                        # the usual default
        assert not alert_on({"alertTypes": {kind: True}}, kind)   # even asked for


def test_alert_on_defaults_to_on_and_honours_a_false():
    for cfg in ({}, None, {"alertTypes": None}, {"alertTypes": "junk"},
                {"alertTypes": {}}):
        assert all(alert_on(cfg, k) for k in ALERT_KEYS)
    # a settings.json written before this alert existed must not silence it
    cfg = {"alertTypes": {"brief": False}}
    assert not alert_on(cfg, "brief")
    assert all(alert_on(cfg, k) for k in ALERT_KEYS if k != "brief")
    # only an explicit False is off — truthy junk stays on
    assert alert_on({"alertTypes": {"summary_close": 1}}, "summary_close")


def test_summary_run_picks_its_switch_from_the_clock():
    """Both summary runs share one function; the session decides which key."""
    import daily_summary as ds

    class ET:
        def __init__(self, h, m=0):
            self.hour, self.minute = h, m

        def date(self):
            return "d"

    seen = {}

    def fake_read(bucket, name, default=None):
        seen["read"] = seen.get("read", []) + [name]
        return {"alertTypes": {"summary_premkt": False}} \
            if name == "settings.json" else default

    orig = (ds._eastern_now, ds.is_trading_day, ds._read_json)
    ds.is_trading_day, ds._read_json = (lambda d: True), fake_read
    try:
        ds._eastern_now = lambda: ET(8, 35)          # trendalert-summary-premkt
        assert ds.run_daily_summary("b")["summary"] == "skipped(off:summary_premkt)"
        assert "data.json" not in seen.get("read", [])   # bails before the read
        ds._eastern_now = lambda: ET(16, 50)         # …-summary-close is still on
        assert ds.run_daily_summary("b")["summary"] == "error(no-data.json)"
    finally:
        ds._eastern_now, ds.is_trading_day, ds._read_json = orig


# ----------------------------------------------------------------------
# Settings > Alerts: the text under the poster, and who else gets the email
# ----------------------------------------------------------------------
def test_caption_text_is_off_until_it_is_asked_for():
    """The inverse of alert_on's fail-open rule, and deliberately so."""
    for cfg in ({}, None, "not-a-dict", {"captionText": False},
                {"captionText": "yes"}, {"captionText": 1}):
        assert caption_text_on(cfg) is False
    assert caption_text_on({"captionText": True}) is True


def test_caption_carries_the_signals_only_when_switched_on():
    from daily_summary import summary_text, signal_text, DASHBOARD_URL
    s = {"core_name": "Core", "label": "L", "session": "MARKET CLOSE",
         "action": [("AAA", "1.0% from 52w high")], "status": {"AAA": "AT HIGH"}}
    assert summary_text(s) == DASHBOARD_URL                 # default
    verbose = summary_text(s, True)
    assert verbose.startswith("TrendAlert Daily · Market Close")
    assert "⚡ ACTION (1)" in verbose and "[AT HIGH]" in verbose
    assert verbose.endswith(DASHBOARD_URL)                  # link still rides along
    assert signal_text(s) in verbose
    assert summary_text(None, True) == DASHBOARD_URL        # nothing to say -> link


def test_recipients_come_from_env_and_ignore_settings():
    """settings.json is public, so a recipient list stored there is a leak.

    The argument is still accepted (every call site passes it) but must have
    no effect -- including for an old settings.json that still carries the key.
    """
    orig = dict(os.environ)
    os.environ["SMTP_USER"] = "me@example.com"
    os.environ["ALERT_TO"] = "me@example.com"
    try:
        assert email_recipients(None) == ["me@example.com"]
        assert email_recipients({}) == ["me@example.com"]
        assert email_recipients({"alertEmails": ["you@example.com"]}) == ["me@example.com"]
        assert email_recipients("not-a-dict") == ["me@example.com"]
        os.environ["ALERT_TO"] = "a@example.com, b@example.com"
        assert email_recipients(None) == ["a@example.com", "b@example.com"]
        del os.environ["ALERT_TO"]
        assert email_recipients(None) == ["me@example.com"]   # falls back to SMTP_USER
    finally:
        os.environ.clear()
        os.environ.update(orig)


def test_one_channel_failing_does_not_stop_the_other():
    def boom():
        raise RuntimeError("smtp down")
    out = fan_out({}, (("telegram", lambda: "tg-photo:sent(1/1)"),
                       ("email", boom)))
    assert out == "tg-photo:sent(1/1)+email:error(RuntimeError)"
