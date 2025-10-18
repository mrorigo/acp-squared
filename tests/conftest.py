from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport

from acp2_proxy import create_app
from acp2_proxy.settings import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Generator[None, None, None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def auth_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "test-token"
    monkeypatch.setenv("ACP2_AUTH_TOKEN", token)
    return token


@pytest.fixture()
def agents_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    agent_script = Path(__file__).parent / "dummy_agent.py"
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "test": {
                    "name": "test",
                    "command": [sys.executable, str(agent_script)],
                    "api_key": "sk-test-1234567890abcdef",
                    "description": "Test agent for ACP² development.",
                    "version": "0.1.0",
                },
                "codex-acp": {
                    "name": "codex-acp",
                    "command": [sys.executable, str(agent_script)],
                    "description": "Dummy testing agent",
                    "version": "0.0.0",
                }
            }
        )
    )
    monkeypatch.setenv("ACP2_AGENTS_CONFIG", str(config_path))
    return config_path


@pytest.fixture()
def client(auth_token: str, agents_config: Path) -> Generator[TestClient, None, None]:
    app = create_app()
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {auth_token}"})
        yield test_client


@pytest.fixture()
async def async_client(auth_token: str, agents_config: Path):
    app = create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as test_client:
            test_client.headers.update({"Authorization": f"Bearer {auth_token}"})
            yield test_client


@pytest.fixture()
def anyio_backend():
    return "asyncio"
