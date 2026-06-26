"""Shared fixtures: a deterministic in-memory service and FastAPI test client."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from llming_com.courier.config import Settings
from llming_com.courier.server.app import create_app
from llming_com.courier.service import ExchangeService
from llming_com.courier.storage.memory import InMemoryBackend

API_KEY = "test-upload-key"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        api_keys={API_KEY},
        public_base_url="http://testserver",
        container="exchange",
        signing_key="test-signing-secret",
        default_single_use=False,
    )


@pytest.fixture
def backend() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.fixture
def service(backend: InMemoryBackend, settings: Settings) -> ExchangeService:
    return ExchangeService(backend, settings)


@pytest.fixture
def client(service: ExchangeService, settings: Settings) -> TestClient:
    app = create_app(settings=settings, service=service)
    return TestClient(app)
