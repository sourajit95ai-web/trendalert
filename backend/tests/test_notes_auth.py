"""Write-token tests — the guard that makes the dashboard safe to hand out.

The endpoint is deployed --allow-unauthenticated because the dashboard is a
static page with no login, so the token IS the whole access control on writes.
These tests pin the three things that matter: reads stay open, writes without
a good token never reach the bucket, and a missing ADMIN_TOKEN fails closed
rather than reverting to the old open-to-the-internet behaviour.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import pytest

import notes_function
from notes_function import notes, TOKEN_HEADER

GOOD = "s3cret-token-value"


class FakeRequest:
    """Just the surface notes() touches: method, args, headers, get_json."""

    def __init__(self, method="POST", headers=None, json_body=None, args=None):
        self.method = method
        self.headers = headers or {}
        self.args = args or {}
        self._json = json_body or {}

    def get_json(self, silent=False):
        return self._json


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", GOOD)


@pytest.fixture
def no_writes(monkeypatch):
    """Blow up if anything tries to touch the bucket.

    The gate is meant to run BEFORE any read or write, so a rejected request
    that still reaches storage is a bug even if it returns the right status.
    """
    def boom(*a, **k):
        raise AssertionError("storage touched on an unauthorized request")
    monkeypatch.setattr(notes_function, "_read", boom)
    monkeypatch.setattr(notes_function, "_write", boom)


def post(headers=None, kind="settings"):
    return FakeRequest(method="POST", headers=headers or {},
                       json_body={"kind": kind, "data": {"gainPct": 99}})


def test_post_without_token_is_rejected(token_env, no_writes):
    body, status, _ = notes(post())
    assert status == 401
    assert json.loads(body)["error"] == "unauthorized"


def test_post_with_wrong_token_is_rejected(token_env, no_writes):
    body, status, _ = notes(post({TOKEN_HEADER: "not-the-password"}))
    assert status == 401


def test_post_with_empty_token_header_is_rejected(token_env, no_writes):
    _, status, _ = notes(post({TOKEN_HEADER: ""}))
    assert status == 401


def test_unset_admin_token_fails_closed(monkeypatch, no_writes):
    """No secret wired => nobody writes, not everybody writes."""
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    body, status, _ = notes(post({TOKEN_HEADER: GOOD}))
    assert status == 503
    assert "not configured" in json.loads(body)["error"]


def test_trailing_newline_in_the_secret_still_matches(monkeypatch):
    """`echo pw | gcloud secrets create` stores a trailing newline.

    Without the strip that stores a secret nobody can ever match, and the
    symptom is a 401 identical to a wrong password — an afternoon of
    debugging for an invisible character.
    """
    monkeypatch.setenv("ADMIN_TOKEN", GOOD + "\n")
    monkeypatch.setattr(notes_function, "_read", lambda kind: {})
    monkeypatch.setattr(notes_function, "_write", lambda kind, data: None)
    _, status, _ = notes(post({TOKEN_HEADER: GOOD}))
    assert status == 200


def test_whitespace_only_secret_counts_as_unconfigured(monkeypatch, no_writes):
    monkeypatch.setenv("ADMIN_TOKEN", "   \n")
    _, status, _ = notes(post({TOKEN_HEADER: "   "}))
    assert status == 503


def test_passphrase_with_spaces_inside_works(monkeypatch):
    """Internal spaces are fine — only the ends are trimmed."""
    phrase = "harbor cobalt maple ledger"
    monkeypatch.setenv("ADMIN_TOKEN", phrase)
    monkeypatch.setattr(notes_function, "_read", lambda kind: {})
    monkeypatch.setattr(notes_function, "_write", lambda kind, data: None)
    _, status, _ = notes(post({TOKEN_HEADER: phrase}))
    assert status == 200


def test_good_token_writes(token_env, monkeypatch):
    seen = {}
    monkeypatch.setattr(notes_function, "_read", lambda kind: {})
    monkeypatch.setattr(notes_function, "_write",
                        lambda kind, data: seen.update({kind: data}))
    body, status, _ = notes(post({TOKEN_HEADER: GOOD}))
    assert status == 200
    assert seen["settings"]["gainPct"] == 99.0


def test_every_write_kind_is_gated(token_env, no_writes):
    """No POST branch may be reachable without the token."""
    for kind in ("notes", "positions", "settings", "universe", "pbre", "core"):
        _, status, _ = notes(post(kind=kind))
        assert status == 401, f"{kind} accepted an unauthenticated write"


def test_get_stays_open(monkeypatch):
    """Reads need no token — this data is already public in the bucket."""
    monkeypatch.setenv("ADMIN_TOKEN", GOOD)
    monkeypatch.setattr(notes_function, "_read", lambda kind: ["MSFT"])
    body, status, _ = notes(FakeRequest(method="GET", args={"kind": "universe"}))
    assert status == 200
    assert json.loads(body)["universe"] == ["MSFT"]


def test_preflight_advertises_the_token_header(token_env):
    """Without this the browser blocks the write before sending it."""
    _, status, headers = notes(FakeRequest(method="OPTIONS"))
    assert status == 204
    assert TOKEN_HEADER in headers["Access-Control-Allow-Headers"]
