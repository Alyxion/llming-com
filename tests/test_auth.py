"""Tests for llming_com.auth — HMAC cookie signing and verification."""

import pytest
from llming_com.auth import (
    AUTH_COOKIE_NAME,
    IDENTITY_COOKIE_NAME,
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


class TestSignAuthToken:
    def test_roundtrip(self):
        token = sign_auth_token("session-123")
        assert "." in token
        parts = token.rsplit(".", 1)
        assert parts[0] == "session-123"

    def test_different_sessions_different_tokens(self):
        t1 = sign_auth_token("s1")
        t2 = sign_auth_token("s2")
        assert t1 != t2

    def test_same_session_same_token(self):
        t1 = sign_auth_token("s1")
        t2 = sign_auth_token("s1")
        assert t1 == t2


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
        # Tamper with last char
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        req = FakeRequest({AUTH_COOKIE_NAME: tampered})
        assert verify_auth_cookie(req) is False

    def test_no_dot_in_token(self):
        req = FakeRequest({AUTH_COOKIE_NAME: "nodothere"})
        assert verify_auth_cookie(req) is False

    def test_empty_token(self):
        req = FakeRequest({AUTH_COOKIE_NAME: ""})
        assert verify_auth_cookie(req) is False


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
        # Second part is a timestamp (integer)
        assert parts[1].isdigit()

    def test_tampered(self):
        token = sign_identity_token("oauth-42")
        tampered = token[:-1] + "X"
        req = FakeRequest({IDENTITY_COOKIE_NAME: tampered})
        assert verify_identity_cookie(req) is None

    def test_missing(self):
        req = FakeRequest({})
        assert verify_identity_cookie(req) is None

    def test_expired_token(self):
        import time as _time
        token = sign_identity_token("oauth-exp")
        # Manually craft an expired token (8 days ago)
        parts = token.split(".")
        old_ts = int(_time.time()) - 8 * 86400
        from llming_com.auth import _AUTH_SECRET
        import hashlib, hmac
        payload = f"id:oauth-exp:{old_ts}"
        sig = hmac.new(_AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        expired_token = f"oauth-exp.{old_ts}.{sig}"
        req = FakeRequest({IDENTITY_COOKIE_NAME: expired_token})
        assert verify_identity_cookie(req) is None

    def test_legacy_two_part_token(self):
        """Legacy 2-part tokens (no timestamp) should still verify."""
        from llming_com.auth import _AUTH_SECRET
        import hashlib, hmac
        identity_id = "legacy-user"
        sig = hmac.new(
            _AUTH_SECRET.encode(), f"id:{identity_id}".encode(), hashlib.sha256
        ).hexdigest()[:32]
        legacy_token = f"{identity_id}.{sig}"
        req = FakeRequest({IDENTITY_COOKIE_NAME: legacy_token})
        assert verify_identity_cookie(req) == "legacy-user"


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
