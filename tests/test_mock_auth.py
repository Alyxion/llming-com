"""Tests for llming_com.mock_auth -- mock user registry."""

import pytest

from llming_com.mock_auth import (
    _mock_sessions,
    get_mock_profile,
    is_registered_mock_user,
    register_mock_user,
)


class MockUserProfile:
    """Minimal mock profile for testing without office_mcp dependency."""
    def __init__(self, user_id: str, email: str, display_name: str = ""):
        self.user_id = user_id
        self.email = email
        self.display_name = display_name


@pytest.fixture(autouse=True)
def clean_registry():
    _mock_sessions.clear()
    yield
    _mock_sessions.clear()


class TestRegisterMockUser:
    def test_register(self):
        profile = MockUserProfile("u1", "alice@test.com", "Alice")
        register_mock_user("alice@test.com", profile)
        assert is_registered_mock_user("alice@test.com")

    def test_case_insensitive_registration(self):
        profile = MockUserProfile("u1", "Bob@Test.COM", "Bob")
        register_mock_user("Bob@Test.COM", profile)
        assert is_registered_mock_user("bob@test.com")
        assert is_registered_mock_user("BOB@TEST.COM")

    def test_overwrite_existing(self):
        p1 = MockUserProfile("u1", "alice@test.com", "Alice v1")
        p2 = MockUserProfile("u1", "alice@test.com", "Alice v2")
        register_mock_user("alice@test.com", p1)
        register_mock_user("alice@test.com", p2)
        result = get_mock_profile("alice@test.com")
        assert result.display_name == "Alice v2"


class TestGetMockProfile:
    def test_found(self):
        profile = MockUserProfile("u1", "alice@test.com", "Alice")
        register_mock_user("alice@test.com", profile)
        result = get_mock_profile("alice@test.com")
        assert result is profile

    def test_not_found(self):
        assert get_mock_profile("nobody@test.com") is None

    def test_case_insensitive_lookup(self):
        profile = MockUserProfile("u1", "alice@test.com")
        register_mock_user("alice@test.com", profile)
        assert get_mock_profile("Alice@Test.COM") is profile


class TestIsRegisteredMockUser:
    def test_registered(self):
        register_mock_user("a@b.com", MockUserProfile("u1", "a@b.com"))
        assert is_registered_mock_user("a@b.com") is True

    def test_not_registered(self):
        assert is_registered_mock_user("x@y.com") is False
