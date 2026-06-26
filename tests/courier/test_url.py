"""Capability-URL assembly/parsing, with the key kept in the fragment."""

from __future__ import annotations

from llming_com.courier.crypto import generate_key
from llming_com.courier.url import build_capability_url, parse_capability_url


def test_build_and_parse_with_key():
    key = generate_key()
    url = build_capability_url(
        "https://acct.blob.core.windows.net",
        "exchange",
        "abc123",
        "se=1700000000&sig=deadbeef",
        key=key,
    )
    assert url.startswith("https://acct.blob.core.windows.net/exchange/abc123?")
    assert "#k=" in url

    parsed = parse_capability_url(url)
    assert parsed.container == "exchange"
    assert parsed.object_id == "abc123"
    assert parsed.key == key
    # The dereferenced object URL drops the secret fragment.
    assert "#" not in parsed.object_url
    assert parsed.object_url.endswith("?se=1700000000&sig=deadbeef")


def test_key_never_in_object_url():
    key = generate_key()
    url = build_capability_url("https://h", "c", "id", "q=1", key=key)
    parsed = parse_capability_url(url)
    # The key only appears after '#', so a fragment-stripped GET cannot leak it.
    assert "k=" not in parsed.object_url


def test_parse_without_key():
    url = build_capability_url("https://h", "c", "id", "q=1", key=None)
    assert "#" not in url
    assert parse_capability_url(url).key is None


def test_single_segment_path():
    parsed = parse_capability_url("https://host/objectid?se=1")
    assert parsed.object_id == "objectid"
    assert parsed.container == ""
