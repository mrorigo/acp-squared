from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncGenerator, Optional, List, Dict

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse
from fastapi.encoders import jsonable_encoder

from .agent_registry import AgentRegistry
from .database import SessionDatabase
from .logging_config import configure_logging
from .models import AgentManifest, AgentSummary, Run, RunCreateRequest, RunMode, RunStatus, Message, MessagePart
from .run_manager import RunManager
from .session_manager import SessionManager
from .settings import get_settings
from .zed_agent import AgentProcessError, PromptCancelled, ZedAgentConnection

logger = logging.getLogger(__name__)


def format_sse(event: str, data: Any) -> bytes:
    """Serialize data as a server-sent event."""
    encoded = jsonable_encoder(data)
    return f"event: {event}\ndata: {json.dumps(encoded)}\n\n".encode("utf-8")


def require_authorization(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency enforcing bearer token authentication."""
    settings = get_settings()
    token = settings.auth_token
    if not token:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    provided = authorization.split(" ", 1)[1]
    if provided != token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()

    # Initialize components
    app.state.registry = AgentRegistry()
    app.state.run_manager = RunManager()

    # Load agent configuration
    config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "agents.json")
    with open(config_path, 'r') as f:
        agent_config = json.load(f)

    # Initialize database and session manager
    app.state.database = SessionDatabase()
    app.state.session_manager = SessionManager(app.state.database, agent_config)

    logger.info("ACP² proxy initialized with stateful session support")
    yield

    # Cleanup
    app.state.database.close()
    logger.info("ACP² proxy shutdown")


def get_registry(request: Request) -> AgentRegistry:
    return request.app.state.registry


def get_run_manager(request: Request) -> RunManager:
    return request.app.state.run_manager


def get_session_manager(request: Request) -> SessionManager:
    return request.app.state.session_manager


def get_database(request: Request) -> SessionDatabase:
    return request.app.state.database




def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(title="ACP² Proxy Server", version="0.1.0", lifespan=lifespan)

    @app.get("/ping", dependencies=[Depends(require_authorization)])
    async def ping() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/agents",
        response_model=list[AgentSummary],
        dependencies=[Depends(require_authorization)],
    )
    async def list_agents(registry: AgentRegistry = Depends(get_registry)) -> list[AgentSummary]:
        agents = [
            AgentSummary(name=agent.name, description=agent.description)
            for agent in registry.list()
        ]
        return agents

    @app.get(
        "/agents/{name}",
        response_model=AgentManifest,
        dependencies=[Depends(require_authorization)],
    )
    async def agent_manifest(name: str, registry: AgentRegistry = Depends(get_registry)) -> AgentManifest:
        try:
            return registry.manifest_for(name)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found") from None

    @app.post(
        "/runs",
        dependencies=[Depends(require_authorization)],
    )

    async def create_run_endpoint(
        payload: RunCreateRequest,
        registry: AgentRegistry = Depends(get_registry),
        manager: RunManager = Depends(get_run_manager),
        session_manager: SessionManager = Depends(get_session_manager),
    ):
        try:
            agent = registry.get(payload.agent)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found") from None

        run = await manager.create_run(agent.name, payload.mode)
        # Convert input content to structured content blocks
        prompt_content = [{"type": "text", "text": part.text} for part in payload.input.content]

        # Handle stateful sessions
        acp_session = None
        if payload.session_id:
            # Use existing session if provided
            active_session = await session_manager.get_or_create_session(
                payload.session_id,
                agent.name,
                os.getcwd()  # Use current working directory or allow override
            )
            acp_session = active_session.acp_session
            logger.info("Using stateful session", extra={
                "run_id": run.id,
                "acp_session_id": payload.session_id,
                "zed_session_id": acp_session.zed_session_id
            })

        if payload.mode == RunMode.sync:
            try:
                async with ZedAgentConnection(agent.command, api_key=agent.api_key) as connection:
                    await manager.start_run(run.id, connection)

                    # Initialize ZedACP connection
                    await connection.initialize()

                    # Handle session persistence
                    if acp_session and acp_session.zed_session_id:
                        # Load existing ZedACP session for true stateful behavior
                        try:
                            await connection.load_session(
                                acp_session.zed_session_id,
                                acp_session.working_directory,
                                []  # MCP servers for now
                            )
                            session_id = acp_session.zed_session_id
                            logger.debug("Loaded existing ZedACP session", extra={
                                "run_id": run.id,
                                "acp_session_id": payload.session_id,
                                "zed_session_id": session_id
                            })
                        except Exception as e:
                            logger.warning("Failed to load ZedACP session, creating new", extra={
                                "run_id": run.id,
                                "acp_session_id": payload.session_id,
                                "error": str(e)
                            })
                            # Fallback to new session if load fails
                            session_id = await connection.start_session(
                                cwd=acp_session.working_directory,
                                mcp_servers=[]
                            )
                            # Update mapping with new ZedACP session ID
                            if payload.session_id:
                                await session_manager.db.update_zed_session_id(
                                    payload.session_id, session_id
                                )
                    elif acp_session:
                        # Create new ZedACP session for ACP session
                        session_id = await connection.start_session(
                            cwd=acp_session.working_directory,
                            mcp_servers=[]
                        )
                        # Update mapping with new ZedACP session ID
                        if payload.session_id:
                            await session_manager.db.update_zed_session_id(
                                payload.session_id, session_id
                            )
                        logger.debug("Created new ZedACP session for ACP session", extra={
                            "run_id": run.id,
                            "acp_session_id": payload.session_id,
                            "zed_session_id": session_id
                        })
                    else:
                        # Stateless run - create new session
                        session_id = await connection.start_session(cwd="/Users/origo/src/acp2", mcp_servers=[])

                    await manager.set_session_id(run.id, session_id)
                    cancel_event = await manager.cancel_event_for(run.id)

                    async def on_chunk(text: str) -> None:
                        await manager.append_output_part(run.id, text)
                    logger.debug("About to call connection.prompt with cancel_event", extra={"run_id": run.id, "cancel_event_is_set": cancel_event.is_set()})
                    prompt_task = asyncio.create_task(
                        connection.prompt(session_id, prompt_content, on_chunk=on_chunk, cancel_event=cancel_event)
                    )
                    cancel_wait = asyncio.create_task(cancel_event.wait())
                    done, pending = await asyncio.wait(
                        {prompt_task, cancel_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    cancelled = cancel_wait in done or cancel_event.is_set()
                    result: dict[str, Any] | None = None

                    if prompt_task in done:
                        try:
                            result = prompt_task.result()
                        except PromptCancelled:
                            cancelled = True
                        except AgentProcessError:
                            raise

                    # Cancel remaining tasks safely, handling different return types
                    for task in pending:
                        if not task.done():
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass

                    if cancelled:
                        if not prompt_task.done():
                            prompt_task.cancel()
                            await asyncio.gather(prompt_task, return_exceptions=True)
                            try:
                                await connection.cancel(session_id)
                            except AgentProcessError:
                                logger.warning("Failed to send cancellation to agent", extra={"run_id": run.id})
                        else:
                            await asyncio.gather(prompt_task, return_exceptions=True)
                        cancelled_run = await manager.cancel_run(run.id)
                        return cancelled_run

                    cancel_wait.cancel()
                    await asyncio.gather(cancel_wait, return_exceptions=True)

                    if cancel_event.is_set():
                        cancelled_run = await manager.cancel_run(run.id)
                        return cancelled_run

                    result = result or {}
                    stop_reason = result.get("stopReason") if isinstance(result, dict) else None
                    completed = await manager.complete_run(run.id, stop_reason)

                    # Store message history for stateful sessions
                    if payload.session_id and acp_session:
                        # Store the user input message
                        await session_manager.append_message_to_history(
                            acp_session_id=payload.session_id,
                            run_id=run.id,
                            message=payload.input,
                            sequence_number=0  # User message is first
                        )

                        # Store the assistant response if available
                        if completed.output:
                            await session_manager.append_message_to_history(
                                acp_session_id=payload.session_id,
                                run_id=run.id,
                                message=completed.output,
                                sequence_number=1  # Assistant response is second
                            )

                        # Update session activity
                        await session_manager.update_session_activity(payload.session_id, run.id)

                    return completed
            except AgentProcessError as exc:  # pragma: no cover - error path
                logger.exception("Agent process failed during sync run", extra={"run_id": run.id})
                failed = await manager.fail_run(run.id, str(exc))
                error_message = failed.error.message if failed.error and failed.error.message else str(exc)
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=error_message)

        # streaming mode
        async def event_stream() -> AsyncGenerator[bytes, None]:
            queue: asyncio.Queue[bytes | None] = asyncio.Queue()
            cancelled_emitted = False
            cancel_event_ref: asyncio.Event | None = None

            # Track message parts for history storage
            message_parts = []

            async def emit(event: str, data: Any) -> None:
                logger.debug("Emitting SSE", extra={"event": event})
                if event == "run.started":
                    logger.info("run.started emitted", extra={"run_id": data["id"] if isinstance(data, dict) and "id" in data else None})
                # Put the event in queue immediately for real-time streaming
                await queue.put(format_sse(event, data))

                # Collect message parts for history storage
                if event == "message.part" and isinstance(data, dict):
                    part_text = data.get("delta", {}).get("text", "")
                    if part_text:
                        message_parts.append(part_text)

            async def process_agent() -> None:
                nonlocal cancelled_emitted, cancel_event_ref
                try:
                    async with ZedAgentConnection(agent.command, api_key=agent.api_key) as connection:
                        await manager.start_run(run.id, connection)
                        logger.info("process_agent before run.started", extra={"run_id": run.id})
                        await emit("run.started", run.model_dump(mode="json"))
                        logger.info("process_agent after run.started", extra={"run_id": run.id})
                        await connection.initialize()
                        session_id = await connection.start_session(cwd="/Users/origo/src/acp2", mcp_servers=[])
                        await manager.set_session_id(run.id, session_id)
                        cancel_event = await manager.cancel_event_for(run.id)
                        cancel_event_ref = cancel_event

                        async def on_chunk(text: str) -> None:
                            await manager.append_output_part(run.id, text)
                            await emit(
                                "message.part",
                                {"run_id": run.id, "delta": {"type": "text", "text": text}},
                            )

                        prompt_task = asyncio.create_task(
                            connection.prompt(session_id, prompt_content, on_chunk=on_chunk, cancel_event=cancel_event)
                        )

                        cancel_wait = asyncio.create_task(cancel_event.wait())

                        try:
                            done, pending = await asyncio.wait(
                                {prompt_task, cancel_wait},
                                return_when=asyncio.FIRST_COMPLETED,
                            )

                            cancelled = cancel_wait in done or cancel_event.is_set()
                            result: dict[str, Any] | None = None

                            if prompt_task in done:
                                try:
                                    result = prompt_task.result()
                                except PromptCancelled:
                                    cancelled = True
                                except AgentProcessError:
                                    raise

                            # Cancel remaining tasks safely, handling different return types
                            for task in pending:
                                if not task.done():
                                    task.cancel()
                                    try:
                                        await task
                                    except asyncio.CancelledError:
                                        pass

                            if cancelled:
                                if not prompt_task.done():
                                    prompt_task.cancel()
                                    try:
                                        await prompt_task
                                    except asyncio.CancelledError:
                                        pass
                                # Always send cancellation to the agent when cancellation is requested
                                try:
                                    await connection.cancel(session_id)
                                except AgentProcessError:
                                    logger.warning(
                                        "Failed to send cancellation to agent",
                                        extra={"run_id": run.id},
                                    )
                                cancelled_run = await manager.cancel_run(run.id)
                                await emit("run.cancelled", cancelled_run.model_dump(mode="json"))
                                cancelled_emitted = True
                            else:
                                if cancel_event.is_set():
                                    # Cancellation was requested but agent completed first
                                    cancelled_run = await manager.cancel_run(run.id)
                                    await emit("run.cancelled", cancelled_run.model_dump(mode="json"))
                                    cancelled_emitted = True
                                else:
                                    result = result or {}
                                    stop_reason = result.get("stopReason") if isinstance(result, dict) else None
                                    completed = await manager.complete_run(run.id, stop_reason)
                                    await emit("run.completed", completed.model_dump(mode="json"))

                                    # Store message history for stateful sessions
                                    if payload.session_id and acp_session and message_parts:
                                        # Store the user input message
                                        await session_manager.append_message_to_history(
                                            acp_session_id=payload.session_id,
                                            run_id=run.id,
                                            message=payload.input,
                                            sequence_number=0  # User message is first
                                        )

                                        # Store the assistant response as a single message
                                        assistant_message = Message(
                                            role="assistant",
                                            content=[MessagePart(type="text", text="".join(message_parts))]
                                        )
                                        await session_manager.append_message_to_history(
                                            acp_session_id=payload.session_id,
                                            run_id=run.id,
                                            message=assistant_message,
                                            sequence_number=1  # Assistant response is second
                                        )

                                        # Update session activity
                                        await session_manager.update_session_activity(payload.session_id, run.id)
                        except Exception as e:
                            logger.exception("Error in prompt processing", extra={"run_id": run.id, "error": str(e)})
                            raise
                except PromptCancelled:
                    cancelled = await manager.cancel_run(run.id)
                    await emit("run.cancelled", cancelled.model_dump(mode="json"))
                    cancelled_emitted = True
                except AgentProcessError as exc:
                    logger.exception("Agent process failed during streaming run", extra={"run_id": run.id})
                    failed = await manager.fail_run(run.id, str(exc))
                    await emit("run.failed", failed.model_dump(mode="json"))
                finally:
                    await queue.put(None)

            agent_task = asyncio.create_task(process_agent())
            try:
                # Stream events as they come in - this keeps the connection open
                while True:
                    try:
                        # Wait for events with a timeout to allow for cancellation checks
                        item = await asyncio.wait_for(queue.get(), timeout=0.1)
                        if item is None:
                            break
                        yield item
                    except asyncio.TimeoutError:
                        # Check if we need to handle post-completion cancellation
                        if agent_task.done():
                            break
                        continue

                # After the agent process is done, check if cancellation was requested
                # but not yet emitted (handles race condition where cancellation happens
                # after agent completes but before we check cancel_event)
                logger.info("Checking for post-completion cancellation", extra={"run_id": run.id, "cancel_event_ref": cancel_event_ref is not None})
                if cancel_event_ref is None:
                    cancel_event_ref = await manager.cancel_event_for(run.id)
                    logger.info("Retrieved cancel_event_ref", extra={"run_id": run.id, "cancel_event_is_set": cancel_event_ref.is_set()})
                if cancel_event_ref.is_set() and not cancelled_emitted:
                    logger.info("post-completion cancellation detected", extra={"run_id": run.id})
                    cancelled_run = await manager.cancel_run(run.id)
                    yield format_sse("run.cancelled", cancelled_run.model_dump(mode="json"))
                else:
                    logger.info("No post-completion cancellation", extra={"run_id": run.id, "cancel_event_is_set": cancel_event_ref.is_set(), "cancel_event_id": id(cancel_event_ref)})
            finally:
                # Clean up the agent task
                if not agent_task.done():
                    agent_task.cancel()
                    try:
                        await agent_task
                    except asyncio.CancelledError:
                        pass

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post(
        "/runs/{run_id}/cancel",
        response_model=Run,
        dependencies=[Depends(require_authorization)],
    )
    async def cancel_run(
        run_id: str,
        manager: RunManager = Depends(get_run_manager),
    ) -> Run:
        try:
            run = await manager.get_run(run_id)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found") from None

        connection = await manager.connection_for(run_id)
        response_run = await manager.request_cancel(run_id)
        cancel_event = await manager.cancel_event_for(run_id)
        logger.info(
            "Run marked for cancellation",
            extra={"run_id": run_id, "has_connection": connection is not None, "cancel_event_set": cancel_event.is_set(), "cancel_event_id": id(cancel_event)},
        )
        return response_run

    # Session management endpoints
    @app.get(
        "/sessions",
        dependencies=[Depends(require_authorization)],
    )
    async def list_sessions(
        agent_name: Optional[str] = None,
        active_only: bool = True,
        session_manager: SessionManager = Depends(get_session_manager),
    ) -> List[Dict[str, Any]]:
        """List ACP sessions with optional filtering."""
        sessions = await session_manager.list_acp_sessions(agent_name, active_only)
        return [
            {
                "session_id": session.acp_session_id,
                "agent_name": session.agent_name,
                "zed_session_id": session.zed_session_id,
                "working_directory": session.working_directory,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "is_active": session.is_active,
                "last_run_id": session.last_run_id,
            }
            for session in sessions
        ]

    @app.get(
        "/sessions/{session_id}",
        dependencies=[Depends(require_authorization)],
    )
    async def get_session(
        session_id: str,
        session_manager: SessionManager = Depends(get_session_manager),
        database: SessionDatabase = Depends(get_database),
    ) -> Dict[str, Any]:
        """Get detailed information about an ACP session."""
        session = await session_manager.get_acp_session(session_id)
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        # Get session history
        history = await database.get_session_history(session_id)

        return {
            "session_id": session.acp_session_id,
            "agent_name": session.agent_name,
            "zed_session_id": session.zed_session_id,
            "working_directory": session.working_directory,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "is_active": session.is_active,
            "last_run_id": session.last_run_id,
            "message_count": len(history),
            "history": [
                {
                    "run_id": msg.run_id,
                    "role": msg.message_role,
                    "created_at": msg.created_at.isoformat(),
                    "sequence_number": msg.sequence_number,
                }
                for msg in history
            ]
        }

    @app.delete(
        "/sessions/{session_id}",
        dependencies=[Depends(require_authorization)],
    )
    async def delete_session(
        session_id: str,
        session_manager: SessionManager = Depends(get_session_manager),
    ) -> Dict[str, str]:
        """Delete an ACP session and its associated data."""
        deleted = await session_manager.delete_acp_session(session_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        return {"deleted": session_id}

    return app
