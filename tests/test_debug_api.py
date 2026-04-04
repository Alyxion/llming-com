"""Tests for llming_com.debug — debug API router."""

import os
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llming_com.debug import build_debug_router
from llming_com.session import BaseSessionEntry, BaseSessionRegistry


@dataclass
class DebugEntry(BaseSessionEntry):
    score: int = 0


class DebugRegistry(BaseSessionRegistry["DebugEntry"]):
    pass


@pytest.fixture(autouse=True)
def reset():
    DebugRegistry.reset()
    yield
    DebugRegistry.reset()


@pytest.fixture
def api_key():
    key = "test-debug-key-12345"
    os.environ["TEST_DEBUG_KEY"] = key
    yield key
    os.environ.pop("TEST_DEBUG_KEY", None)


@pytest.fixture
def app(api_key):
    reg = DebugRegistry.get()
    # Register some test sessions
    reg.register("s1", DebugEntry(user_id="u1", user_name="Alice", score=10))
    reg.register("s2", DebugEntry(user_id="u2", user_name="Bob", score=20))

    def detail_hook(sid, entry):
        return {"score": entry.score}

    router = build_debug_router(
        reg,
        api_key_env="TEST_DEBUG_KEY",
        prefix="",  # no prefix for simpler test URLs
        allowed_networks=["*"],  # skip IP check for testing
    )
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def headers(api_key):
    return {"x-debug-key": api_key}


class TestDebugAuth:
    def test_missing_key_returns_401(self, client):
        resp = client.get("/sessions")
        assert resp.status_code == 401

    def test_wrong_key_returns_401(self, client):
        resp = client.get("/sessions", headers={"x-debug-key": "wrong"})
        assert resp.status_code == 401

    def test_valid_key_in_header(self, client, headers):
        resp = client.get("/sessions", headers=headers)
        assert resp.status_code == 200

    def test_query_param_key_rejected(self, client, api_key):
        """API key in query params is no longer accepted (header only)."""
        resp = client.get(f"/sessions?key={api_key}")
        assert resp.status_code == 401


class TestListSessions:
    def test_lists_all(self, client, headers):
        resp = client.get("/sessions", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        sids = [s["session_id"] for s in data["sessions"]]
        assert "s1" in sids
        assert "s2" in sids

    def test_session_fields(self, client, headers):
        resp = client.get("/sessions", headers=headers)
        s = next(s for s in resp.json()["sessions"] if s["session_id"] == "s1")
        assert s["user_id"] == "u1"
        assert s["user_name"] == "Alice"
        assert s["ws_connected"] is False
        assert isinstance(s["idle_seconds"], int)


class TestGetSession:
    def test_existing_session(self, client, headers):
        resp = client.get("/sessions/s1", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "s1"
        assert data["user_name"] == "Alice"

    def test_nonexistent_session(self, client, headers):
        resp = client.get("/sessions/nonexistent", headers=headers)
        assert resp.status_code == 404


class TestSessionDetailHook:
    def test_hook_adds_fields(self, api_key):
        reg = DebugRegistry.get()
        reg.register("s3", DebugEntry(user_id="u3", score=99))

        router = build_debug_router(
            reg,
            api_key_env="TEST_DEBUG_KEY",
            prefix="",
            allowed_networks=["*"],
            session_detail_hook=lambda sid, e: {"score": e.score, "extra": True},
        )
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/sessions/s3", headers={"x-debug-key": api_key})
        assert resp.status_code == 200
        data = resp.json()
        assert data["score"] == 99
        assert data["extra"] is True


class TestWsSend:
    def test_no_controller_returns_400(self, client, headers):
        resp = client.post(
            "/sessions/s1/ws_send",
            json={"type": "test"},
            headers=headers,
        )
        assert resp.status_code == 400

    def test_nonexistent_session(self, client, headers):
        resp = client.post(
            "/sessions/nonexistent/ws_send",
            json={"type": "test"},
            headers=headers,
        )
        assert resp.status_code == 404


class TestExtraRoutes:
    def test_extra_routes_registered(self, api_key):
        reg = DebugRegistry.get()
        reg.register("s1", DebugEntry(user_id="u1"))

        def add_extra(router, registry):
            @router.get("/custom")
            async def custom_endpoint():
                return {"custom": True, "count": registry.active_count}

        router = build_debug_router(
            reg,
            api_key_env="TEST_DEBUG_KEY",
            prefix="",
            allowed_networks=["*"],
            extra_routes=add_extra,
        )
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/custom", headers={"x-debug-key": api_key})
        assert resp.status_code == 200
        assert resp.json()["custom"] is True
        assert resp.json()["count"] >= 1
