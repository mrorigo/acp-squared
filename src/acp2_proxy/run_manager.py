from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, TYPE_CHECKING

from .models import ErrorDetail, Message, MessagePart, Run, RunMode, RunStatus

if TYPE_CHECKING:
    from .zed_agent import ZedAgentConnection

logger = logging.getLogger(__name__)


@dataclass
class RunState:
    """Internal bookkeeping for an active run."""

    run: Run
    connection: Optional["ZedAgentConnection"] = None
    session_id: Optional[str] = None
    buffered_parts: list[MessagePart] = field(default_factory=list)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_requested_at: Optional[datetime] = None


class RunManager:
    """Manage the lifecycle of active runs for cancellation and queries."""

    def __init__(self) -> None:
        self._runs: Dict[str, RunState] = {}
        self._lock = asyncio.Lock()

    async def create_run(self, agent: str, mode: RunMode) -> Run:
        """Initialize a run entry with queued status."""
        run_id = str(uuid.uuid4())
        timestamp = datetime.now(tz=timezone.utc)
        run = Run(
            id=run_id,
            agent=agent,
            mode=mode,
            status=RunStatus.queued,
            created_at=timestamp,
            updated_at=timestamp,
        )
        async with self._lock:
            self._runs[run_id] = RunState(run=run)
        logger.debug("Created run", extra={"run_id": run_id, "agent": agent, "mode": mode})
        return run

    async def start_run(self, run_id: str, connection: "ZedAgentConnection") -> None:
        """Mark run as in progress and associate connection."""
        async with self._lock:
            state = self._runs[run_id]
            state.run.status = RunStatus.in_progress
            state.run.updated_at = datetime.now(tz=timezone.utc)
            state.connection = connection

    async def set_session_id(self, run_id: str, session_id: str) -> None:
        async with self._lock:
            self._runs[run_id].session_id = session_id

    async def append_output_part(self, run_id: str, text: str) -> None:
        async with self._lock:
            state = self._runs[run_id]
            part = MessagePart(text=text)
            state.buffered_parts.append(part)
            logger.debug("Appended output part", extra={
                "run_id": run_id,
                "text_length": len(text),
                "text_preview": text[:50] + "..." if len(text) > 50 else text,
                "total_parts": len(state.buffered_parts)
            })

    async def complete_run(self, run_id: str, stop_reason: str | None = None) -> Run:
        async with self._lock:
            state = self._runs[run_id]
            state.run.status = RunStatus.completed
            state.run.stop_reason = stop_reason
            state.run.updated_at = datetime.now(tz=timezone.utc)
            if state.buffered_parts:
                state.run.output = Message(role="assistant", content=list(state.buffered_parts))
                logger.debug("Completed run with output", extra={
                    "run_id": run_id,
                    "parts_count": len(state.buffered_parts),
                    "total_text_length": sum(len(part.text) for part in state.buffered_parts),
                    "output_content_preview": state.run.output.content[0].text[:100] + "..." if state.run.output.content else "No content"
                })
            else:
                logger.warning("Completed run with no buffered parts", extra={
                    "run_id": run_id,
                    "stop_reason": stop_reason
                })
            state.connection = None
            return state.run

    async def fail_run(self, run_id: str, error: str, code: str = "agent_error") -> Run:
        async with self._lock:
            state = self._runs[run_id]
            state.run.status = RunStatus.failed
            state.run.updated_at = datetime.now(tz=timezone.utc)
            state.run.error = ErrorDetail(code=code, message=error)
            state.connection = None
            return state.run

    async def cancel_run(self, run_id: str) -> Run:
        async with self._lock:
            state = self._runs.get(run_id)
            if not state:
                raise KeyError(run_id)
            state.run.status = RunStatus.cancelled
            state.run.updated_at = datetime.now(tz=timezone.utc)
            state.connection = None
            return state.run

    async def request_cancel(self, run_id: str, *, timeout: float | None = None) -> Run:
        async with self._lock:
            state = self._runs[run_id]
            if state.run.status != RunStatus.cancelling:
                state.run.status = RunStatus.cancelling
                state.run.updated_at = datetime.now(tz=timezone.utc)
            state.cancel_requested_at = datetime.now(tz=timezone.utc)
            state.cancel_event.set()
            logger.debug("Cancellation requested", extra={"run_id": run_id})
            return state.run.model_copy(deep=True)

    async def get_run(self, run_id: str) -> Run:
        async with self._lock:
            state = self._runs[run_id]
            return state.run

    async def pop(self, run_id: str) -> None:
        async with self._lock:
            removed = self._runs.pop(run_id, None)
        logger.debug("Run removed", extra={"run_id": run_id})

    async def connection_for(self, run_id: str) -> Optional["ZedAgentConnection"]:
        async with self._lock:
            state = self._runs.get(run_id)
            if not state:
                return None
            return state.connection

    async def session_for(self, run_id: str) -> Optional[str]:
        async with self._lock:
            state = self._runs.get(run_id)
            if not state:
                return None
            return state.session_id

    async def wait_for_session(self, run_id: str, timeout: float = 5.0) -> Optional[str]:
        """Wait for the session identifier to become available."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            session_id = await self.session_for(run_id)
            if session_id is not None:
                return session_id
            if asyncio.get_event_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.05)

    async def cancel_event_for(self, run_id: str) -> asyncio.Event:
        async with self._lock:
            state = self._runs.get(run_id)
            if not state:
                raise KeyError(run_id)
            return state.cancel_event

