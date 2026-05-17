"""End-to-end tests for llming_com.access.remote."""

from __future__ import annotations

import base64
import json
import threading

from fastapi.testclient import TestClient

from llming_com.access.remote import InMemoryAccessStore, create_access_app


def _logged_in_clients() -> tuple[TestClient, TestClient, dict]:
    store = InMemoryAccessStore()
    store.create_user("admin", "Password123!", "Admin")
    app = create_access_app(store)
    browser_client = TestClient(app)
    tunnel_client = TestClient(app)
    login = browser_client.post(
        "/api/access/login",
        json={"username": "admin", "password": "Password123!"},
    )
    assert login.status_code == 200
    host = browser_client.post("/api/access/hosts", json={"display_name": "Lab Mac"}).json()
    return browser_client, tunnel_client, host


def test_http_request_is_proxied_through_host_tunnel() -> None:
    browser_client, tunnel_client, host = _logged_in_clients()

    with tunnel_client.websocket_connect(f"/api/access/tunnel?key={host['connection_key']}") as tunnel:
        assert tunnel.receive_json()["type"] == "welcome"

        result: dict[str, object] = {}

        def request_proxy() -> None:
            result["response"] = browser_client.get(f"/proxy/{host['host_id']}/hello?x=1")

        thread = threading.Thread(target=request_proxy)
        thread.start()

        request = tunnel.receive_json()
        assert request["type"] == "http_request"
        assert request["method"] == "GET"
        assert request["path"] == "/hello?x=1"
        assert request["headers"]["x-forwarded-via"] == "proxy"

        tunnel.send_json({
            "type": "http_response",
            "req_id": request["req_id"],
            "status": 200,
            "headers": {"content-type": "text/plain"},
            "body_b64": base64.b64encode(b"hello from inside").decode("ascii"),
        })
        thread.join(timeout=3)

        response = result["response"]
        assert response.status_code == 200
        assert response.text == "hello from inside"


def test_html_proxy_injects_base_href() -> None:
    browser_client, tunnel_client, host = _logged_in_clients()

    with tunnel_client.websocket_connect(f"/api/access/tunnel?key={host['connection_key']}") as tunnel:
        tunnel.receive_json()
        result: dict[str, object] = {}
        thread = threading.Thread(
            target=lambda: result.setdefault(
                "response",
                browser_client.get(f"/proxy/{host['host_id']}/"),
            )
        )
        thread.start()

        request = tunnel.receive_json()
        tunnel.send_json({
            "type": "http_response",
            "req_id": request["req_id"],
            "status": 200,
            "headers": {"content-type": "text/html"},
            "body_b64": base64.b64encode(b"<html><head></head><body>ok</body></html>").decode("ascii"),
        })
        thread.join(timeout=3)

        response = result["response"]
        assert response.status_code == 200
        assert f'<base href="/proxy/{host["host_id"]}/">' in response.text


def test_websocket_frames_are_piped_through_host_tunnel() -> None:
    browser_client, tunnel_client, host = _logged_in_clients()

    with tunnel_client.websocket_connect(f"/api/access/tunnel?key={host['connection_key']}") as tunnel:
        tunnel.receive_json()
        with browser_client.websocket_connect(f"/proxy/{host['host_id']}/ws/echo?mode=test") as browser_ws:
            open_msg = tunnel.receive_json()
            assert open_msg["type"] == "ws_open"
            assert open_msg["path"] == "/ws/echo?mode=test"
            ws_id = open_msg["ws_id"]

            browser_ws.send_text("from browser")
            browser_msg = tunnel.receive_json()
            assert browser_msg == {
                "type": "ws_data",
                "ws_id": ws_id,
                "text": "from browser",
            }

            tunnel.send_json({"type": "ws_data", "ws_id": ws_id, "text": "from host"})
            assert browser_ws.receive_text() == "from host"

            browser_ws.send_bytes(b"\x00\x01")
            binary_msg = tunnel.receive_json()
            assert binary_msg["type"] == "ws_data"
            assert binary_msg["ws_id"] == ws_id
            assert base64.b64decode(binary_msg["binary"]) == b"\x00\x01"


def test_qr_token_login_is_verified_by_host_not_public_hub() -> None:
    browser_client, tunnel_client, host = _logged_in_clients()

    with tunnel_client.websocket_connect(f"/api/access/tunnel?key={host['connection_key']}") as tunnel:
        tunnel.receive_json()
        result: dict[str, object] = {}
        thread = threading.Thread(
            target=lambda: result.setdefault(
                "response",
                browser_client.get(f"/t/{host['host_id']}/short-lived-token", follow_redirects=False),
            )
        )
        thread.start()

        request = tunnel.receive_json()
        assert request["type"] == "http_request"
        assert request["method"] == "POST"
        assert request["path"] == "/_llming/remote/verify-token"
        assert json.loads(request["body"]) == {"token": "short-lived-token"}

        tunnel.send_json({
            "type": "http_response",
            "req_id": request["req_id"],
            "status": 200,
            "headers": {"content-type": "application/json"},
            "body_b64": base64.b64encode(b'{"valid": true}').decode("ascii"),
        })
        thread.join(timeout=3)

        response = result["response"]
        assert response.status_code == 302
        assert response.headers["location"].endswith(f"/proxy/{host['host_id']}/")
