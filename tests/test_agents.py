from __future__ import annotations

from fastapi.testclient import TestClient


def test_ping(client: TestClient) -> None:
    response = client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_agents(client: TestClient) -> None:
    response = client.get("/agents")
    assert response.status_code == 200
    payload = response.json()
    # Should return all configured agents
    assert len(payload) >= 1
    agent_names = [agent["name"] for agent in payload]
    assert "test" in agent_names  # Our test agent should be present


def test_agent_manifest(client: TestClient) -> None:
    response = client.get("/agents/test")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "test"
    assert data["capabilities"]["modes"] == ["sync", "stream"]
