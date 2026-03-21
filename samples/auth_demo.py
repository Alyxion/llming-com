"""Authentication demo.

Shows HMAC token signing, verification, and identity tokens with TTL.
"""

from llming_com import (
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
    """Simulates a request object with cookies."""
    def __init__(self, cookies: dict):
        self.cookies = cookies


def main():
    # ── Session auth tokens ───────────────────────────────────────────
    print("=== Session Auth Tokens ===")

    # Generate a session ID + signed token
    session_id, token = make_auth_cookie_value()
    print(f"Session ID: {session_id}")
    print(f"Token:      {token}")

    # Verify the token
    request = FakeRequest({AUTH_COOKIE_NAME: token})
    print(f"Valid:      {verify_auth_cookie(request)}")
    print(f"Extracted:  {get_auth_session_id(request)}")

    # Tampered token fails
    tampered = token[:-1] + ("x" if token[-1] != "x" else "y")
    bad_request = FakeRequest({AUTH_COOKIE_NAME: tampered})
    print(f"Tampered:   {verify_auth_cookie(bad_request)}")

    # ── Identity tokens (with TTL) ────────────────────────────────────
    print("\n=== Identity Tokens ===")

    identity_token = sign_identity_token("oauth-user-42")
    print(f"Identity token: {identity_token}")

    request = FakeRequest({IDENTITY_COOKIE_NAME: identity_token})
    identity_id = verify_identity_cookie(request, max_age=604800)  # 7 days
    print(f"Identity ID:    {identity_id}")

    # Manual token signing
    print("\n=== Manual Token Signing ===")
    token = sign_auth_token("my-custom-session")
    print(f"Signed: {token}")
    print(f"Same input = same output: {sign_auth_token('my-custom-session') == token}")
    print(f"Different input: {sign_auth_token('other-session') != token}")


if __name__ == "__main__":
    main()
