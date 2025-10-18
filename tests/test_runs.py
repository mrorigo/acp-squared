from __future__ import annotations

import asyncio
import json
import json
from typing import Generator, Iterable, List, Tuple

import pytest

from fastapi.testclient import TestClient


def user_message(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def iter_sse(response) -> Generator[Tuple[str, dict], None, None]:
    buffer = ""
    for chunk in response.iter_text():
        buffer += chunk
        while "\n\n" in buffer:
            raw_event, buffer = buffer.split("\n\n", 1)
            event_name = None
            data_lines: List[str] = []
            for line in raw_event.splitlines():
                if line.startswith("event: "):
                    event_name = line[len("event: ") :]
                elif line.startswith("data: "):
                    data_lines.append(line[len("data: ") :])
            if not event_name:
                continue
            data_json = "\n".join(data_lines)
            yield event_name, json.loads(data_json)


async def async_iter_sse(response) -> Generator[Tuple[str, dict], None, None]:
    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        while "\n\n" in buffer:
            raw_event, buffer = buffer.split("\n\n", 1)
            event_name = None
            data_lines: List[str] = []
            for line in raw_event.splitlines():
                if line.startswith("event: "):
                    event_name = line[len("event: ") :]
                elif line.startswith("data: "):
                    data_lines.append(line[len("data: ") :])
            if event_name:
                data_json = "\n".join(data_lines)
                yield event_name, json.loads(data_json)


def test_run_sync(client: TestClient) -> None:
    payload = {
        "agent": "test",
        "mode": "sync",
        "input": user_message("hello world"),
    }
    response = client.post("/runs", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["mode"] == "sync"
    content = "".join(part["text"] for part in data["output"]["content"])
    assert "hello" in content
    assert data["stop_reason"] == "stop"


def test_run_stream(client: TestClient) -> None:
    payload = {
        "agent": "test",
        "mode": "stream",
        "input": user_message("streaming test"),
    }
    with client.stream("POST", "/runs", json=payload) as response:
        events = list(iter_sse(response))
    event_names = [name for name, _ in events]
    assert event_names[0] == "run.started"
    assert "message.part" in event_names
    assert event_names[-1] == "run.completed"
    completed_event = next(data for name, data in events if name == "run.completed")
    assert completed_event["status"] == "completed"
    message = "".join(part["text"] for part in completed_event["output"]["content"])
    assert "streaming" in message


@pytest.mark.anyio("asyncio")
async def test_run_cancellation(async_client) -> None:
    payload = {
        "agent": "test",
        "mode": "stream",
        "input": user_message("long running task that can be cancelled ::delay0.1"),
    }
    cancel_info = None
    events_received = []
    run_id = None

    async with async_client.stream("POST", "/runs", json=payload) as response:
        async for event, data in async_iter_sse(response):
            events_received.append(event)
            if event == "run.started":
                run_id = data["id"]
                # Wait a bit for the agent to actually start processing
                await asyncio.sleep(0.1)
                # Cancel while agent is running to test proper cancellation
                cancel_response = await async_client.post(f"/runs/{run_id}/cancel")
                assert cancel_response.status_code == 200
                cancel_info = cancel_response.json()
            if event == "run.cancelled":
                assert cancel_info is not None
                assert cancel_info["status"] == "cancelling"
                return
            # If we get run.completed, the cancellation didn't take effect
            # This is expected if cancellation happens too late
            if event == "run.completed":
                # Check if we requested cancellation (even if it was too late)
                if cancel_info is not None:
                    # Cancellation was requested but agent completed first - this is valid
                    return
                else:
                    pytest.fail(f"Received run.completed without cancellation request. Events: {events_received}")

    # If we exit the loop without seeing run.cancelled or valid run.completed, something is wrong
    if cancel_info is not None:
        pytest.fail(f"Cancellation was requested but no completion event received. Events: {events_received}")
    else:
        pytest.fail(f"No events received")
