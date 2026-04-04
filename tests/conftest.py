"""Shared test fixtures for llming-com."""

import os

import pytest

# Ensure auth secret is always set for tests
os.environ.setdefault("LLMING_AUTH_SECRET", "test-secret-for-ci")


@pytest.fixture(autouse=True)
def _reset_auth_secret():
    """Reset the cached auth secret before each test.

    This ensures that each test starts with a clean secret state,
    picking up whatever LLMING_AUTH_SECRET is set in the environment.
    """
    from llming_com.auth import _reset_secret
    _reset_secret()
    yield
    _reset_secret()
