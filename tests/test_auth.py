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
    AuthManager,
    _reset_default,
    get_auth,
)


class FakeRequest:
    """Minimal request mock with cookies dict."""
    def __init__(self, cookies: dict):
        self.cookies = cookies


# ── AuthManager secret loading ─────────────────────────────────


class TestAuthManagerSecret:
    def test_explicit_secret(self):
        auth = AuthManager(secret="my-secret")
        assert auth.secret == "my-secret"

    def test_reads_env_var(self):
        os.environ["LLMING_AUTH_SECRET"] = "env-secret"
        _reset_default()
        auth = AuthManager()
        assert auth.secret == "env-secret"

    def test_dev_env_generates_random(self):
        os.environ.pop("LLMING_AUTH_SECRET", None)
        os.environ["LLMING_ENV"] = "dev"
        auth = AuthManager()
        assert auth.secret.startswith("llming_dev_")
        os.environ["LLMING_ENV"] = ""
        os.environ["LLMING_AUTH_SECRET"] = "test-secret-for-ci"

    def test_production_raises_without_secret(self):
        os.environ.pop("LLMING_AUTH_SECRET", None)
        os.environ.pop("LLMING_ENV", None)
        auth = AuthManager()
        with pytest.raises(RuntimeError, match="LLMING_AUTH_SECRET must be set"):
            _ = auth.secret
        os.environ["LLMING_AUTH_SECRET"] = "test-secret-for-ci"

    def test_caches_result(self):
        auth = AuthManager()
        _reset_default()
        s1 = auth.secret
        s2 = auth.secret
        assert s1 is s2


class TestDefaultSingleton:
    def test_returns_same_instance(self):
        _reset_default()
        a = get_auth()
        b = get_auth()
        assert a is b

    def test_reset_creates_new(self):
        a = get_auth()
        _reset_default()
        b = get_auth()
        assert a is not b


# ── sign_auth_token ──────────────────────────────────────────────


class TestSignAuthToken:
    def setup_method(self):
        self.auth = AuthManager(secret="test-secret")

    def test_roundtrip(self):
        token = self.auth.sign_auth_token("session-123")
        assert "." in token
        parts = token.split(".")
        assert len(parts) == 3
        assert parts[0] == "session-123"

    def test_three_part_format(self):
        token = self.auth.sign_auth_token("abc")
        parts = token.split(".")
        assert len(parts) == 3
        assert parts[0] == "abc"
        assert parts[1].isdigit()
        assert len(parts[2]) == 64

    def test_different_sessions_different_tokens(self):
        t1 = self.auth.sign_auth_token("s1")
        t2 = self.auth.sign_auth_token("s2")
        assert t1 != t2

    def test_full_hmac_digest(self):
        token = self.auth.sign_auth_token("test")
        sig = token.split(".")[-1]
        assert len(sig) == 64

    def test_token_contains_timestamp(self):
        before = int(time.time())
        token = self.auth.sign_auth_token("s1")
        after = int(time.time())
        ts = int(token.split(".")[1])
        assert before <= ts <= after


# ── verify_auth_cookie ───────────────────────────────────────────


class TestVerifyAuthCookie:
    def setup_method(self):
        self.auth = AuthManager(secret="test-secret")

    def test_valid_cookie(self):
        token = self.auth.sign_auth_token("test-session")
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        assert self.auth.verify_auth_cookie(req) is True

    def test_missing_cookie(self):
        req = FakeRequest({})
        assert self.auth.verify_auth_cookie(req) is False

    def test_tampered_signature(self):
        token = self.auth.sign_auth_token("test-session")
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        req = FakeRequest({AUTH_COOKIE_NAME: tampered})
        assert self.auth.verify_auth_cookie(req) is False

    def test_no_dot_in_token(self):
        req = FakeRequest({AUTH_COOKIE_NAME: "nodothere"})
        assert self.auth.verify_auth_cookie(req) is False

    def test_empty_token(self):
        req = FakeRequest({AUTH_COOKIE_NAME: ""})
        assert self.auth.verify_auth_cookie(req) is False

    def test_expired_token(self):
        old_ts = str(int(time.time()) - 90000)
        payload = f"test-session.{old_ts}"
        sig = hmac.new(b"test-secret", payload.encode(), hashlib.sha256).hexdigest()
        token = f"test-session.{old_ts}.{sig}"
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        assert self.auth.verify_auth_cookie(req) is False

    def test_custom_max_age(self):
        token = self.auth.sign_auth_token("s1")
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        assert self.auth.verify_auth_cookie(req, max_age=1) is True

    def test_expired_custom_max_age(self):
        old_ts = str(int(time.time()) - 5)
        payload = f"s1.{old_ts}"
        sig = hmac.new(b"test-secret", payload.encode(), hashlib.sha256).hexdigest()
        token = f"s1.{old_ts}.{sig}"
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        assert self.auth.verify_auth_cookie(req, max_age=1) is False

    def test_legacy_two_part_token(self):
        sig = hmac.new(b"test-secret", b"legacy-session", hashlib.sha256).hexdigest()
        token = f"legacy-session.{sig}"
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            assert self.auth.verify_auth_cookie(req) is True
            assert any("Legacy 2-part" in str(x.message) for x in w)

    def test_invalid_timestamp(self):
        req = FakeRequest({AUTH_COOKIE_NAME: "sess.notanumber.abcdef"})
        assert self.auth.verify_auth_cookie(req) is False


# ── get_auth_session_id ──────────────────────────────────────────


class TestGetAuthSessionId:
    def setup_method(self):
        self.auth = AuthManager(secret="test-secret")

    def test_valid(self):
        token = self.auth.sign_auth_token("my-session")
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        assert self.auth.get_auth_session_id(req) == "my-session"

    def test_invalid(self):
        req = FakeRequest({AUTH_COOKIE_NAME: "bad.token"})
        assert self.auth.get_auth_session_id(req) is None

    def test_missing(self):
        req = FakeRequest({})
        assert self.auth.get_auth_session_id(req) is None

    def test_expired(self):
        old_ts = str(int(time.time()) - 90000)
        payload = f"s1.{old_ts}"
        sig = hmac.new(b"test-secret", payload.encode(), hashlib.sha256).hexdigest()
        token = f"s1.{old_ts}.{sig}"
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        assert self.auth.get_auth_session_id(req) is None


# ── Identity tokens ──────────────────────────────────────────────


class TestIdentityToken:
    def setup_method(self):
        self.auth = AuthManager(secret="test-secret")

    def test_roundtrip(self):
        token = self.auth.sign_identity_token("oauth-session-42")
        req = FakeRequest({IDENTITY_COOKIE_NAME: token})
        assert self.auth.verify_identity_cookie(req) == "oauth-session-42"

    def test_token_has_three_parts(self):
        token = self.auth.sign_identity_token("oauth-session-42")
        parts = token.split(".")
        assert len(parts) == 3
        assert parts[0] == "oauth-session-42"
        assert parts[1].isdigit()

    def test_full_hmac_no_truncation(self):
        token = self.auth.sign_identity_token("test")
        sig = token.split(".")[-1]
        assert len(sig) == 64

    def test_tampered(self):
        token = self.auth.sign_identity_token("oauth-42")
        tampered = token[:-1] + "X"
        req = FakeRequest({IDENTITY_COOKIE_NAME: tampered})
        assert self.auth.verify_identity_cookie(req) is None

    def test_missing(self):
        req = FakeRequest({})
        assert self.auth.verify_identity_cookie(req) is None

    def test_expired_token(self):
        # Default identity max_age is 30 days; use 31 to guarantee expiry
        # without coupling the test to the exact TTL.
        old_ts = int(time.time()) - 31 * 86400
        payload = f"id:oauth-exp:{old_ts}"
        sig = hmac.new(b"test-secret", payload.encode(), hashlib.sha256).hexdigest()
        expired_token = f"oauth-exp.{old_ts}.{sig}"
        req = FakeRequest({IDENTITY_COOKIE_NAME: expired_token})
        assert self.auth.verify_identity_cookie(req) is None

    def test_legacy_two_part_token(self):
        sig = hmac.new(
            b"test-secret", b"id:legacy-user", hashlib.sha256
        ).hexdigest()
        legacy_token = f"legacy-user.{sig}"
        req = FakeRequest({IDENTITY_COOKIE_NAME: legacy_token})
        assert self.auth.verify_identity_cookie(req) == "legacy-user"


# ── make_auth_cookie_value ───────────────────────────────────────


class TestMakeAuthCookieValue:
    def setup_method(self):
        self.auth = AuthManager(secret="test-secret")

    def test_returns_tuple(self):
        sid, token = self.auth.make_auth_cookie_value()
        assert isinstance(sid, str)
        assert isinstance(token, str)
        assert len(sid) == 32
        assert "." in token

    def test_token_verifies(self):
        sid, token = self.auth.make_auth_cookie_value()
        req = FakeRequest({AUTH_COOKIE_NAME: token})
        assert self.auth.verify_auth_cookie(req) is True
        assert self.auth.get_auth_session_id(req) == sid

    def test_unique_per_call(self):
        sid1, _ = self.auth.make_auth_cookie_value()
        sid2, _ = self.auth.make_auth_cookie_value()
        assert sid1 != sid2
