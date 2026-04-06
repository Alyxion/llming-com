"""Tests for llming_com.client_static — JS client file serving."""

from pathlib import Path

from llming_com.client_static import STATIC_DIR, mount_client_static


class TestClientStatic:
    def test_static_dir_exists(self):
        assert STATIC_DIR.is_dir()

    def test_llming_ws_js_exists(self):
        js_file = STATIC_DIR / "llming-ws.js"
        assert js_file.is_file()
        content = js_file.read_text()
        assert "class LlmingWebSocket" in content

    def test_llming_ws_js_contains_key_features(self):
        content = (STATIC_DIR / "llming-ws.js").read_text()
        # Reconnect logic
        assert "reconnect" in content.lower()
        assert "_maxReconnectAttempts" in content
        assert "exponential" in content.lower() or "Math.pow" in content
        # Heartbeat with ack timeout
        assert "heartbeat" in content
        assert "heartbeat_ack" in content  # client listens for ack
        assert "_startAckTimeout" in content
        assert "_onHeartbeatAck" in content
        # Session loss codes
        assert "4004" in content
        assert "4001" in content
        # Callbacks
        assert "onSessionLost" in content
        assert "onReconnecting" in content
        assert "onReconnected" in content
        assert "onConnectionWarning" in content
        assert "onConnectionRestored" in content
        # Warning banner
        assert "warningText" in content

    def test_mount_client_static_default_path(self):
        """mount_client_static registers a route and returns the path."""

        class FakeApp:
            def __init__(self):
                self.mounts = []

            def mount(self, path, app, name=""):
                self.mounts.append((path, name))

        app = FakeApp()
        result = mount_client_static(app)
        assert result == "/llming-com"
        assert len(app.mounts) == 1
        assert app.mounts[0][0] == "/llming-com"
        assert app.mounts[0][1] == "llming-com-static"

    def test_mount_client_static_custom_path(self):
        class FakeApp:
            def __init__(self):
                self.mounts = []

            def mount(self, path, app, name=""):
                self.mounts.append((path, name))

        app = FakeApp()
        result = mount_client_static(app, path="/custom/js")
        assert result == "/custom/js"
        assert app.mounts[0][0] == "/custom/js"
