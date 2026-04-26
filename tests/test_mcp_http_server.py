"""Tests for MCP HTTP server security: API key enforcement and localhost restriction."""

import pytest
from unittest.mock import MagicMock, AsyncMock
from starlette.testclient import TestClient
from starlette.applications import Starlette

from llming_com.session import BaseSessionRegistry, BaseSessionEntry


class _DummyRegistry(BaseSessionRegistry[BaseSessionEntry]):
    """Minimal session registry for testing."""

    def create_session(self, **kwargs):
        return "test"

    def get_session(self, session_id):
        return None

    def list_sessions(self):
        return {}


def _make_app(api_key: str = "test-key", localhost_only: bool = True) -> Starlette:
    """Build a minimal Starlette app with MCP mounted."""
    app = Starlette()
    from llming_com.mcp_http_server import mount_mcp_server
    mount_mcp_server(
        app,
        _DummyRegistry(),
        api_key=api_key,
        localhost_only=localhost_only,
        prefix="/mcp",
    )
    return app


# ── API key is mandatory ──────────────────────────────────────────────

def test_mount_requires_api_key():
    """mount_mcp_server must raise if api_key is empty or missing."""
    from llming_com.mcp_http_server import mount_mcp_server
    app = Starlette()
    with pytest.raises(ValueError, match="api_key is required"):
        mount_mcp_server(app, _DummyRegistry(), api_key="")


def test_mount_requires_api_key_none():
    """Passing None for api_key must fail at the type level or raise."""
    from llming_com.mcp_http_server import mount_mcp_server
    app = Starlette()
    with pytest.raises((ValueError, TypeError)):
        mount_mcp_server(app, _DummyRegistry(), api_key=None)


# ── API key enforcement (localhost_only=False to isolate key checks) ──

def test_sse_rejects_missing_key():
    app = _make_app(localhost_only=False)
    client = TestClient(app)
    resp = client.get("/mcp/sse")
    assert resp.status_code == 401


def test_sse_rejects_wrong_key():
    app = _make_app(localhost_only=False)
    client = TestClient(app)
    resp = client.get("/mcp/sse", headers={"x-mcp-key": "wrong-key"})
    assert resp.status_code == 401


def test_post_rejects_missing_key():
    app = _make_app(localhost_only=False)
    client = TestClient(app)
    resp = client.post("/mcp/messages/test", content="{}")
    assert resp.status_code == 401


def test_post_rejects_wrong_key():
    app = _make_app(localhost_only=False)
    client = TestClient(app)
    resp = client.post("/mcp/messages/test", headers={"x-mcp-key": "bad"}, content="{}")
    assert resp.status_code == 401


# ── Localhost restriction ─────────────────────────────────────────────

def test_sse_rejects_non_localhost():
    """With localhost_only=True, non-loopback clients get 403."""
    app = _make_app(localhost_only=True)
    client = TestClient(app)
    # TestClient uses 'testclient' as client host, not 127.0.0.1
    resp = client.get("/mcp/sse", headers={"x-mcp-key": "test-key"})
    assert resp.status_code == 403


def test_post_rejects_non_localhost():
    app = _make_app(localhost_only=True)
    client = TestClient(app)
    resp = client.post(
        "/mcp/messages/test",
        headers={"x-mcp-key": "test-key"},
        content="{}",
    )
    assert resp.status_code == 403


def test_localhost_off_allows_remote():
    """With localhost_only=False, non-loopback clients pass the IP check (still need key)."""
    app = _make_app(localhost_only=False)
    client = TestClient(app)
    # Without key → 401 (not 403), proving the localhost check was skipped
    resp = client.get("/mcp/sse")
    assert resp.status_code == 401
