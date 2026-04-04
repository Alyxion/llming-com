"""Tests for llming_com.auth -- HMAC cookie signing and verification."""

import hashlib
import hmac
import os
import time
import warnings

import pytest

from llming_com.auth import (
    AUTH_COOKIE_NAME,
    IDENTITY_COOKIE_NAME,
    _get_auth_secret,
    _reset_secret,
    get_auth_session_id,
    make_auth_cookie_value,
    sign_auth_token,
    sign_identity_token,
    verify_auth_cookie,
    verify_identity_cookie,
)


class FakeRequest:
    """Minimal request mock with cookies dict."""
    def __init__(self, cookies: dict):
        self.cookies = cookies


# ── Lazy secret loading ──────────────────────────────────────────


class TestGetAuthSecret:
    def test_returns_env_var(self):
        os.environ["LLMING_AUTH_SECRET"] = "my-secret"
        _reset_secret()
        assert _get_auth_secret() == "my-secret"

    def test_dev_env_generates_random(self):
        os.environ.pop("LLMING_AUTH_SECRET", None)
        os.environ["LLMING_ENV"] = "dev"
        _reset_secret()
        secret = _get_auth_secret()
        assert secret.startswith("llming_dev_")
        assert len(secret) > len("llming_dev_")
        os.environ["LLMING_ENV"] = ""
        os.environ["LLMING_AUTH_SECRET"] = "test-secret-for-ci"
        _reset_secret()

    def test_development_env_also_works(self):
        os.environ.pop("LLMING_AUTH_SECRET", None)
        os.environ["LLMING_ENV"] = "development"
        _reset_secret()
        secret = _get_auth_secret()
        assert secret.startswith("llming_dev_")
        os.environ["LLMING_ENV"] = ""
        os.environ["LLMING_AUTH_SECRET"] = "test-secret-for-ci"
        _reset_secret()

    def test_production_raises_without_secret(self):
        os.environ.pop("LLMING_AUTH_SECRET", None)
        os.environ.pop("LLMING_ENV", None)
        _reset_secret()
        with pytest.raises(RuntimeError, match="LLMING_AUTH_SECRET must be set"):
            _get_auth_secret()
        os.environ["LLMING_AUTH_SECRET"] = "test-secret-for-ci"
        _reset_secret()

    def test_caches_result(self):
        _reset_secret()
        s1 = _get_auth_secret()
        s2 = _get_auth_secret()
        assert s1 is s2


# ── sign_auth_token ──────────────────────────────────────────────


class TestSignAuthToken:
    def test_roundtrip(self):
        token = sign_auth_token("session-123")
        assert "." in token
        parts = token.split(".")
        assert len(parts) == 3
        assert parts[0] == "session-123"

    def test_three_part_format(self):
        token = sign_auth_token("abc")
        parts = token.split(".")
        assert len(parts) == 3
        assert parts[0] == "abc"
        assert parts[1].isdigit()  # timestamp
        assert len(parts[2]) == 64  # full SHA-256 hex

    def test_different_sessions_different_tokens(self):
        t1 = sign_auth_token("s1")
        t2 = sign_auth_token("s2")
        assert t1 != t2

    def test_full_hmac_digest(self):
        """Signature must be full 64-char SHA-256, not truncated."""
        token = sign_auth_token("test")
        sig = token.split(".")[-1]
        assert len(sig) == 64

    def test_token_contains_timestamp(self):
        before = int(time.time())
        token = sign_auth_token("s1")
        after = int(time.time())
        ts = int(token.split(".")[1])
        assert before <= ts <= after


# ── verify_auth_cookie ───────────────────────────────────────────


class TestVerifyAuthCookie:
    def test_valid_cookie(self):
        token = sign_auth_token("test-session")
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        assert verify_auth_cookie(req) is True

    def test_missing_cookie(self):
        req = FakeRequest({})
        assert verify_auth_cookie(req) is False

    def test_tampered_signature(self):
        token = sign_auth_token("test-session")
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        req = FakeRequest({AUTH_COOKIE_NAME: tampered})
        assert verify_auth_cookie(req) is False

    def test_no_dot_in_token(self):
        req = FakeRequest({AUTH_COOKIE_NAME: "nodothere"})
        assert verify_auth_cookie(req) is False

    def test_empty_token(self):
        req = FakeRequest({AUTH_COOKIE_NAME: ""})
        assert verify_auth_cookie(req) is False

    def test_expired_token(self):
        """Token older than max_age should fail."""
        secret = _get_auth_secret()
        old_ts = str(int(time.time()) - 90000)  # > 24h ago
        payload = f"test-session.{old_ts}"
        sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        token = f"test-session.{old_ts}.{sig}"
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        assert verify_auth_cookie(req) is False

    def test_custom_max_age(self):
        """Token within custom max_age should pass."""
        token = sign_auth_token("s1")
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        assert verify_auth_cookie(req, max_age=1) is True

    def test_expired_custom_max_age(self):
        """Token created now should fail with max_age=0."""
        secret = _get_auth_secret()
        old_ts = str(int(time.time()) - 5)
        payload = f"s1.{old_ts}"
        sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        token = f"s1.{old_ts}.{sig}"
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        assert verify_auth_cookie(req, max_age=1) is False

    def test_legacy_two_part_token(self):
        """Legacy 2-part tokens should be accepted with deprecation warning."""
        secret = _get_auth_secret()
        sig = hmac.new(secret.encode(), b"legacy-session", hashlib.sha256).hexdigest()
        token = f"legacy-session.{sig}"
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            assert verify_auth_cookie(req) is True
            assert any("Legacy 2-part" in str(x.message) for x in w)

    def test_invalid_timestamp(self):
        req = FakeRequest({AUTH_COOKIE_NAME: "sess.notanumber.abcdef"})
        assert verify_auth_cookie(req) is False


# ── get_auth_session_id ──────────────────────────────────────────


class TestGetAuthSessionId:
    def test_valid(self):
        token = sign_auth_token("my-session")
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        assert get_auth_session_id(req) == "my-session"

    def test_invalid(self):
        req = FakeRequest({AUTH_COOKIE_NAME: "bad.token"})
        assert get_auth_session_id(req) is None

    def test_missing(self):
        req = FakeRequest({})
        assert get_auth_session_id(req) is None

    def test_expired(self):
        secret = _get_auth_secret()
        old_ts = str(int(time.time()) - 90000)
        payload = f"s1.{old_ts}"
        sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        token = f"s1.{old_ts}.{sig}"
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        assert get_auth_session_id(req) is None


# ── Identity tokens ──────────────────────────────────────────────


class TestIdentityToken:
    def test_roundtrip(self):
        token = sign_identity_token("oauth-session-42")
        req = FakeRequest({IDENTITY_COOKIE_NAME: token})
        assert verify_identity_cookie(req) == "oauth-session-42"

    def test_token_has_three_parts(self):
        token = sign_identity_token("oauth-session-42")
        parts = token.split(".")
        assert len(parts) == 3
        assert parts[0] == "oauth-session-42"
        assert parts[1].isdigit()

    def test_full_hmac_no_truncation(self):
        token = sign_identity_token("test")
        sig = token.split(".")[-1]
        assert len(sig) == 64

    def test_tampered(self):
        token = sign_identity_token("oauth-42")
        tampered = token[:-1] + "X"
        req = FakeRequest({IDENTITY_COOKIE_NAME: tampered})
        assert verify_identity_cookie(req) is None

    def test_missing(self):
        req = FakeRequest({})
        assert verify_identity_cookie(req) is None

    def test_expired_token(self):
        secret = _get_auth_secret()
        old_ts = int(time.time()) - 8 * 86400
        payload = f"id:oauth-exp:{old_ts}"
        sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        expired_token = f"oauth-exp.{old_ts}.{sig}"
        req = FakeRequest({IDENTITY_COOKIE_NAME: expired_token})
        assert verify_identity_cookie(req) is None

    def test_legacy_two_part_token(self):
        """Legacy 2-part tokens (no timestamp) should still verify."""
        secret = _get_auth_secret()
        identity_id = "legacy-user"
        sig = hmac.new(
            secret.encode(), f"id:{identity_id}".encode(), hashlib.sha256
        ).hexdigest()
        legacy_token = f"{identity_id}.{sig}"
        req = FakeRequest({IDENTITY_COOKIE_NAME: legacy_token})
        assert verify_identity_cookie(req) == "legacy-user"


# ── make_auth_cookie_value ───────────────────────────────────────


class TestMakeAuthCookieValue:
    def test_returns_tuple(self):
        sid, token = make_auth_cookie_value()
        assert isinstance(sid, str)
        assert isinstance(token, str)
        assert len(sid) == 32  # hex(16 bytes)
        assert "." in token

    def test_token_verifies(self):
        sid, token = make_auth_cookie_value()
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        assert verify_auth_cookie(req) is True
        assert get_auth_session_id(req) == sid

    def test_unique_per_call(self):
        sid1, _ = make_auth_cookie_value()
        sid2, _ = make_auth_cookie_value()
        assert sid1 != sid2
