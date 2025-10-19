"""
Session manager for ACP² stateful agent support.

This module provides session lifecycle management, integrating ACP sessions
with ZedACP's native session persistence capabilities.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, List, TYPE_CHECKING
from pathlib import Path

from .database import SessionDatabase, ACPSession, SessionHistory
from .models import Message

if TYPE_CHECKING:
    from .zed_agent import ZedAgentConnection

logger = logging.getLogger(__name__)


@dataclass
class ActiveSession:
    """Active ACP session with ZedACP connection."""
    acp_session: ACPSession
    zed_connection: Optional["ZedAgentConnection"] = None
    is_loading: bool = False


class SessionManager:
    """
    Manages ACP sessions with ZedACP integration.

    Provides session lifecycle management including creation, persistence,
    and cleanup of ACP sessions that map to ZedACP sessions.
    """

    def __init__(self, database: SessionDatabase, agent_config: dict):
        """Initialize session manager."""
        self.db = database
        self.agent_config = agent_config
        self.active_sessions: Dict[str, ActiveSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_create_session(
        self,
        acp_session_id: str,
        agent: str,
        cwd: str
    ) -> ActiveSession:
        """
        Get existing ACP session or create new one.

        For existing sessions, loads the ZedACP session using session/load.
        For new sessions, creates a new ZedACP session and maps it.
        """
        async with self._lock:
            # Check if session already exists
            existing_session = await self.db.get_acp_session(acp_session_id)
            if existing_session:
                logger.debug("Found existing ACP session", extra={
                    "acp_session_id": acp_session_id,
                    "zed_session_id": existing_session.zed_session_id,
                    "agent": existing_session.agent_name
                })

                # Update last activity
                existing_session.updated_at = datetime.utcnow()
                await self.db.create_acp_session(
                    acp_session_id=existing_session.acp_session_id,
                    agent=existing_session.agent_name,
                    cwd=existing_session.working_directory,
                    zed_session_id=existing_session.zed_session_id,
                    metadata=existing_session.metadata
                )

                return ActiveSession(acp_session=existing_session)

            # Create new session
            logger.debug("Creating new ACP session", extra={
                "acp_session_id": acp_session_id,
                "agent": agent,
                "cwd": cwd
            })

            # Generate initial ZedACP session ID (will be updated when connection is made)
            zed_session_id = f"zed_{acp_session_id}"

            acp_session = await self.db.create_acp_session(
                acp_session_id=acp_session_id,
                agent=agent,
                cwd=cwd,
                zed_session_id=zed_session_id
            )

            active_session = ActiveSession(acp_session=acp_session)
            self.active_sessions[acp_session_id] = active_session

            return active_session

    async def create_ephemeral_session(self, agent: str) -> ActiveSession:
        """Create a temporary session for stateless runs."""
        import uuid
        acp_session_id = f"temp_{uuid.uuid4()}"
        cwd = os.getcwd()

        return await self.get_or_create_session(acp_session_id, agent, cwd)

    async def link_zed_session(
        self,
        acp_session_id: str,
        zed_connection: "ZedAgentConnection",
        zed_session_id: str
    ) -> None:
        """Link ACP session with ZedACP session connection."""
        async with self._lock:
            if acp_session_id in self.active_sessions:
                self.active_sessions[acp_session_id].zed_connection = zed_connection

                # Update database with actual ZedACP session ID
                await self.db.update_zed_session_id(acp_session_id, zed_session_id)

                logger.debug("Linked ZedACP session", extra={
                    "acp_session_id": acp_session_id,
                    "zed_session_id": zed_session_id
                })

    async def get_acp_session(self, acp_session_id: str) -> Optional[ACPSession]:
        """Get ACP session by ID."""
        return await self.db.get_acp_session(acp_session_id)

    async def list_acp_sessions(
        self,
        agent_name: Optional[str] = None,
        active_only: bool = True
    ) -> List[ACPSession]:
        """List ACP sessions with optional filtering."""
        sessions = await self.db.list_acp_sessions(agent_name, active_only)

        # Add runtime information for active sessions
        for session in sessions:
            if session.acp_session_id in self.active_sessions:
                active_session = self.active_sessions[session.acp_session_id]
                session.last_run_id = active_session.acp_session.last_run_id

        return sessions

    async def append_message_to_history(
        self,
        acp_session_id: str,
        run_id: str,
        message: Message,
        sequence_number: int,
        zed_message: Optional[Dict] = None
    ) -> None:
        """Append message to session history."""
        await self.db.append_message_history(
            acp_session_id=acp_session_id,
            run_id=run_id,
            message=message,
            sequence_number=sequence_number,
            zed_message=zed_message
        )

        # Update session's last activity
        session = await self.db.get_acp_session(acp_session_id)
        if session:
            session.updated_at = datetime.utcnow()
            await self.db.create_acp_session(
                acp_session_id=session.acp_session_id,
                agent=session.agent_name,
                cwd=session.working_directory,
                zed_session_id=session.zed_session_id,
                metadata=session.metadata
            )

    async def get_session_history(
        self,
        acp_session_id: str,
        limit: Optional[int] = None
    ) -> List[SessionHistory]:
        """Get message history for a session."""
        return await self.db.get_session_history(acp_session_id, limit)

    async def delete_acp_session(self, acp_session_id: str) -> bool:
        """Delete ACP session and cleanup resources."""
        async with self._lock:
            # Remove from active sessions if present
            self.active_sessions.pop(acp_session_id, None)

            # Delete from database
            deleted = await self.db.delete_acp_session(acp_session_id)

            if deleted:
                logger.debug("Deleted ACP session", extra={"acp_session_id": acp_session_id})

            return deleted

    async def cleanup_old_sessions(self, days_old: int = 30) -> int:
        """Clean up old inactive sessions."""
        deleted_count = await self.db.cleanup_inactive_sessions(days_old)
        logger.debug("Cleaned up old sessions", extra={"deleted_count": deleted_count})
        return deleted_count

    async def update_session_activity(self, acp_session_id: str, run_id: str) -> None:
        """Update session's last activity timestamp and run ID."""
        with self.db._get_connection() as conn:
            conn.execute("""
                UPDATE acp_sessions
                SET updated_at = ?, last_run_id = ?
                WHERE acp_session_id = ?
            """, (datetime.utcnow().isoformat(), run_id, acp_session_id))
            conn.commit()

    def get_agent_config(self, agent_name: str) -> Dict:
        """Get agent configuration from loaded config."""
        return self.agent_config.get(agent_name, {})

    async def health_check(self) -> Dict:
        """Perform health check on session manager."""
        try:
            # Test database connectivity
            sessions = await self.list_acp_sessions(active_only=False, agent_name=None)
            session_count = len(sessions)

            # Count active sessions
            active_count = len(self.active_sessions)

            return {
                "status": "healthy",
                "total_sessions": session_count,
                "active_sessions": active_count,
                "database_path": self.db.db_path
            }
        except Exception as e:
            logger.error("Session manager health check failed", extra={"error": str(e)})
            return {
                "status": "unhealthy",
                "error": str(e)
            }