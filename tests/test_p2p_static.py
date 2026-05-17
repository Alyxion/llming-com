"""Tests for generic P2P browser assets."""

from llming_com.server import STATIC_DIR


def test_p2p_viewer_assets_exist():
    p2p_dir = STATIC_DIR / "p2p"
    assert (p2p_dir / "pair.html").is_file()
    assert (p2p_dir / "app.html").is_file()
    assert (p2p_dir / "llming-p2p-viewer.js").is_file()


def test_pairing_url_uses_fragment_token_only():
    pair_html = (STATIC_DIR / "p2p" / "pair.html").read_text()
    viewer_js = (STATIC_DIR / "p2p" / "llming-p2p-viewer.js").read_text()
    assert "redeemPairingToken" in pair_html
    assert "window.location.hash" in viewer_js
    assert "history.replaceState" in viewer_js
    assert "pairing_token" in viewer_js


def test_app_shell_can_use_stored_credentials_for_handshake():
    app_html = (STATIC_DIR / "p2p" / "app.html").read_text()
    viewer_js = (STATIC_DIR / "p2p" / "llming-p2p-viewer.js").read_text()
    assert "initApp" in app_html
    assert "indexedDB" in viewer_js
    assert "localStorage" in viewer_js
    assert "/connect" in viewer_js
    assert "/response?h=" in viewer_js
