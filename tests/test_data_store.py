"""Tests for llming_com.data_store — SessionDataStore."""

import pytest
from llming_com.data_store import SessionDataStore


@pytest.fixture(autouse=True)
def clean_store():
    SessionDataStore.clear_all()
    yield
    SessionDataStore.clear_all()


class TestSessionDataStore:
    def test_put_and_get(self):
        SessionDataStore.put("assets", "img1", b"\x89PNG")
        assert SessionDataStore.get("assets", "img1") == b"\x89PNG"

    def test_get_missing_key(self):
        assert SessionDataStore.get("assets", "nope") is None

    def test_get_missing_namespace(self):
        assert SessionDataStore.get("nonexistent", "key") is None

    def test_pop(self):
        SessionDataStore.put("ns", "k", "value")
        assert SessionDataStore.pop("ns", "k") == "value"
        assert SessionDataStore.get("ns", "k") is None

    def test_pop_missing(self):
        assert SessionDataStore.pop("ns", "missing") is None

    def test_list_keys(self):
        SessionDataStore.put("ns", "a", 1)
        SessionDataStore.put("ns", "b", 2)
        SessionDataStore.put("ns", "c", 3)
        keys = SessionDataStore.list_keys("ns")
        assert sorted(keys) == ["a", "b", "c"]

    def test_list_keys_empty_namespace(self):
        assert SessionDataStore.list_keys("empty") == []

    def test_clear_namespace(self):
        SessionDataStore.put("ns1", "a", 1)
        SessionDataStore.put("ns1", "b", 2)
        SessionDataStore.put("ns2", "c", 3)
        count = SessionDataStore.clear_namespace("ns1")
        assert count == 2
        assert SessionDataStore.get("ns1", "a") is None
        assert SessionDataStore.get("ns2", "c") == 3

    def test_clear_nonexistent_namespace(self):
        assert SessionDataStore.clear_namespace("nope") == 0

    def test_clear_all(self):
        SessionDataStore.put("ns1", "a", 1)
        SessionDataStore.put("ns2", "b", 2)
        SessionDataStore.clear_all()
        assert SessionDataStore.get("ns1", "a") is None
        assert SessionDataStore.get("ns2", "b") is None

    def test_overwrite(self):
        SessionDataStore.put("ns", "k", "old")
        SessionDataStore.put("ns", "k", "new")
        assert SessionDataStore.get("ns", "k") == "new"

    def test_different_namespaces_isolated(self):
        SessionDataStore.put("a", "key", "from_a")
        SessionDataStore.put("b", "key", "from_b")
        assert SessionDataStore.get("a", "key") == "from_a"
        assert SessionDataStore.get("b", "key") == "from_b"

    def test_binary_data(self):
        data = bytes(range(256))
        SessionDataStore.put("bin", "blob", data)
        assert SessionDataStore.get("bin", "blob") == data

    def test_complex_values(self):
        SessionDataStore.put("ns", "dict", {"nested": [1, 2, 3]})
        assert SessionDataStore.get("ns", "dict") == {"nested": [1, 2, 3]}
