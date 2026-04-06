"""Authentication demo.

Shows HMAC token signing, verification, and identity tokens with TTL.

    LLMING_AUTH_SECRET=my-secret python samples/auth_demo.py
"""

from llming_com import get_auth, AUTH_COOKIE_NAME, IDENTITY_COOKIE_NAME


class FakeRequest:
    """Simulates a request object with cookies."""
    def __init__(self, cookies: dict):
        self.cookies = cookies


def main():
    auth = get_auth()

    # ── Session auth tokens ───────────────────────────────────────────
    print("=== Session Auth Tokens ===")

    session_id, token = auth.make_auth_cookie_value()
    print(f"Session ID: {session_id}")
    print(f"Token:      {token}")

    request = FakeRequest({AUTH_COOKIE_NAME: token})
    print(f"Valid:      {auth.verify_auth_cookie(request)}")
    print(f"Extracted:  {auth.get_auth_session_id(request)}")

    tampered = token[:-1] + ("x" if token[-1] != "x" else "y")
    bad_request = FakeRequest({AUTH_COOKIE_NAME: tampered})
    print(f"Tampered:   {auth.verify_auth_cookie(bad_request)}")

    # ── Identity tokens (with TTL) ────────────────────────────────────
    print("\n=== Identity Tokens ===")

    identity_token = auth.sign_identity_token("oauth-user-42")
    print(f"Identity token: {identity_token}")

    request = FakeRequest({IDENTITY_COOKIE_NAME: identity_token})
    identity_id = auth.verify_identity_cookie(request, max_age=604800)
    print(f"Identity ID:    {identity_id}")

    # Manual token signing
    print("\n=== Manual Token Signing ===")
    token = auth.sign_auth_token("my-custom-session")
    print(f"Signed: {token}")


if __name__ == "__main__":
    main()
