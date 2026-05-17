"""Tests for packaged P2P server deployment assets."""

from pathlib import Path

import llming_com


def test_cloudflare_relay_is_nested_under_server_p2p_relay():
    package_root = Path(llming_com.__file__).parent
    relay_dir = package_root / "server" / "p2p" / "relay" / "cloudflare"

    assert relay_dir.is_dir()
    assert (relay_dir / "index.js").is_file()
    assert (relay_dir / "wrangler.toml").is_file()
    assert (relay_dir / "wrangler.test.toml").is_file()
    assert (relay_dir / ".env.example").is_file()
    assert (relay_dir / "scripts" / "setup-dns.sh").is_file()
    assert (relay_dir / "scripts" / "run-test-relay.sh").is_file()
    assert (relay_dir / "scripts" / "disable-test-relay.sh").is_file()


def test_cloudflare_apps_is_nested_under_server_p2p_apps():
    package_root = Path(llming_com.__file__).parent
    apps_dir = package_root / "server" / "p2p" / "apps" / "cloudflare"

    assert apps_dir.is_dir()
    assert (apps_dir / "index.js").is_file()
    assert (apps_dir / "wrangler.test.toml").is_file()


def test_cloudflare_env_example_names_apps_and_relay_hosts():
    package_root = Path(llming_com.__file__).parent
    env_example = package_root / "server" / "p2p" / "relay" / "cloudflare" / ".env.example"
    text = env_example.read_text()

    assert "LLMING_APPS_HOST" in text
    assert "LLMING_RELAY_HOST" in text
    assert "LLMING_TEST_APPS_HOST" in text
    assert "LLMING_TEST_RELAY_HOST" in text
