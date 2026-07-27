"""Alert delivery channel — Settings > Alerts (settings.json "alertChannel").

The switch lives server-side so it applies to every SCHEDULED alert regardless
of which browser last saved settings. These tests pin the two things that
matter: an unset/garbled value must never silence the alerts, and switching a
channel off must not stop the other one from sending.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alerts_email import alert_channel, channel_on, fan_out


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


def test_one_channel_failing_does_not_stop_the_other():
    def boom():
        raise RuntimeError("smtp down")
    out = fan_out({}, (("telegram", lambda: "tg-photo:sent(1/1)"),
                       ("email", boom)))
    assert out == "tg-photo:sent(1/1)+email:error(RuntimeError)"
