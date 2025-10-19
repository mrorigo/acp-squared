"""
Database layer for ACP² session persistence.

This module provides SQLite-based storage for ACP session mapping and message history,
enabling stateful agent conversations across multiple runs.
"""

import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from src.acp2_proxy.models import Message


@dataclass
class ACPSession:
    """Represents an ACP session with ZedACP mapping."""
    acp_session_id: str
    agent_name: str
    zed_session_id: str
    working_directory: str
    created_at: datetime
    updated_at: datetime
    is_active: bool = True
    last_run_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat()
        data['updated_at'] = self.updated_at.isoformat()
        data['metadata'] = json.dumps(self.metadata) if self.metadata else None
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ACPSession':
        """Create from dictionary."""
        data['created_at'] = datetime.fromisoformat(data['created_at'])
        data['updated_at'] = datetime.fromisoformat(data['updated_at'])
        data['metadata'] = json.loads(data['metadata']) if data['metadata'] else None
        return cls(**data)


@dataclass
class SessionHistory:
    """Represents a message in session history."""
    id: Optional[int]
    acp_session_id: str
    run_id: str
    message_role: str  # 'user' | 'assistant'
    message_data: Dict[str, Any]  # Full IBM ACP Message
    created_at: datetime
    sequence_number: int
    zed_message_data: Optional[Dict[str, Any]] = None  # ZedACP format

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat()
        data['message_data'] = json.dumps(self.message_data)
        data['zed_message_data'] = json.dumps(self.zed_message_data) if self.zed_message_data else None
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SessionHistory':
        """Create from dictionary."""
        data['created_at'] = datetime.fromisoformat(data['created_at'])
        data['message_data'] = json.loads(data['message_data'])
        data['zed_message_data'] = json.loads(data['zed_message_data']) if data['zed_message_data'] else None
        return cls(**data)


class SessionDatabase:
    """
    SQLite database for ACP session persistence.

    Uses WAL mode for better concurrency and creates tables as needed.
    Thread-safe through connection pooling.
    """

    def __init__(self, db_path: str = "acp2_sessions.db"):
        """Initialize database connection."""
        self.db_path = db_path
        self._local = threading.local()
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, 'connection'):
            self._local.connection = sqlite3.connect(
                self.db_path,
                check_same_thread=False
            )
            # Enable WAL mode for better concurrency
            self._local.connection.execute("PRAGMA journal_mode=WAL")
            self._local.connection.execute("PRAGMA synchronous=NORMAL")
            self._local.connection.execute("PRAGMA cache_size=10000")
            self._local.connection.execute("PRAGMA temp_store=memory")

        return self._local.connection

    def _init_database(self) -> None:
        """Initialize database schema."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS acp_sessions (
                    acp_session_id TEXT PRIMARY KEY,
                    agent_name TEXT NOT NULL,
                    zed_session_id TEXT NOT NULL,
                    working_directory TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    last_run_id TEXT,
                    metadata TEXT
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    acp_session_id TEXT NOT NULL REFERENCES acp_sessions(acp_session_id),
                    run_id TEXT NOT NULL,
                    message_role TEXT NOT NULL,
                    message_data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sequence_number INTEGER,
                    zed_message_data TEXT
                )
            """)

            # Create indexes for performance
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_acp_sessions_agent_active
                ON acp_sessions(agent_name, is_active)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_session_history_acp_session
                ON session_history(acp_session_id, created_at)
            """)

            conn.commit()

    async def create_acp_session(
        self,
        acp_session_id: str,
        agent: str,
        cwd: str,
        zed_session_id: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> ACPSession:
        """Create a new ACP session record."""
        now = datetime.utcnow()
        session = ACPSession(
            acp_session_id=acp_session_id,
            agent_name=agent,
            zed_session_id=zed_session_id,
            working_directory=cwd,
            created_at=now,
            updated_at=now,
            metadata=metadata
        )

        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO acp_sessions
                (acp_session_id, agent_name, zed_session_id, working_directory,
                 created_at, updated_at, is_active, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session.acp_session_id,
                session.agent_name,
                session.zed_session_id,
                session.working_directory,
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
                session.is_active,
                json.dumps(session.metadata) if session.metadata else None
            ))
            conn.commit()

        return session

    async def get_acp_session(self, acp_session_id: str) -> Optional[ACPSession]:
        """Retrieve an ACP session by ID."""
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM acp_sessions WHERE acp_session_id = ?
            """, (acp_session_id,))

            row = cursor.fetchone()
            if row:
                return ACPSession.from_dict(dict(row))
            return None

    async def update_zed_session_id(
        self,
        acp_session_id: str,
        zed_session_id: str
    ) -> None:
        """Update the ZedACP session ID for an ACP session."""
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE acp_sessions
                SET zed_session_id = ?, updated_at = ?
                WHERE acp_session_id = ?
            """, (zed_session_id, datetime.utcnow().isoformat(), acp_session_id))
            conn.commit()

    async def append_message_history(
        self,
        acp_session_id: str,
        run_id: str,
        message: Message,
        sequence_number: int,
        zed_message: Optional[Dict[str, Any]] = None
    ) -> None:
        """Append a message to session history."""
        # Determine role from message (this is a simplified approach)
        # In practice, the role should be determined by the context of the run
        # For now, we'll use a simple heuristic based on sequence number
        message_role = "user" if sequence_number == 0 else "assistant"

        history_entry = SessionHistory(
            id=None,
            acp_session_id=acp_session_id,
            run_id=run_id,
            message_role=message_role,
            message_data={
                "role": message.role,
                "content": [{"type": part.type, "text": part.text} for part in message.content]
            },
            created_at=datetime.utcnow(),
            sequence_number=sequence_number,
            zed_message_data=zed_message
        )

        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO session_history
                (acp_session_id, run_id, message_role, message_data, created_at,
                 sequence_number, zed_message_data)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                history_entry.acp_session_id,
                history_entry.run_id,
                history_entry.message_role,
                json.dumps(history_entry.message_data),
                history_entry.created_at.isoformat(),
                history_entry.sequence_number,
                json.dumps(history_entry.zed_message_data) if history_entry.zed_message_data else None
            ))
            conn.commit()

    async def get_session_history(
        self,
        acp_session_id: str,
        limit: Optional[int] = None
    ) -> List[SessionHistory]:
        """Retrieve message history for a session."""
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            query = """
                SELECT * FROM session_history
                WHERE acp_session_id = ?
                ORDER BY sequence_number ASC
            """
            params = [acp_session_id]

            if limit:
                query += " LIMIT ?"
                params.append(str(limit))

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

            return [SessionHistory.from_dict(dict(row)) for row in rows]

    async def list_acp_sessions(
        self,
        agent_name: Optional[str] = None,
        active_only: bool = True
    ) -> List[ACPSession]:
        """List ACP sessions with optional filtering."""
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM acp_sessions WHERE 1=1"
            params = []

            if agent_name:
                query += " AND agent_name = ?"
                params.append(agent_name)

            if active_only:
                query += " AND is_active = 1"

            query += " ORDER BY updated_at DESC"

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

            return [ACPSession.from_dict(dict(row)) for row in rows]

    async def delete_acp_session(self, acp_session_id: str) -> bool:
        """Delete an ACP session and all its history."""
        with self._get_connection() as conn:
            # Delete session history first (foreign key constraint)
            conn.execute("DELETE FROM session_history WHERE acp_session_id = ?", (acp_session_id,))

            # Delete the session
            cursor = conn.execute("DELETE FROM acp_sessions WHERE acp_session_id = ?", (acp_session_id,))
            conn.commit()

            return cursor.rowcount > 0

    async def cleanup_inactive_sessions(self, days_old: int = 30) -> int:
        """Clean up old inactive sessions. Returns number of sessions deleted."""
        cutoff_date = datetime.utcnow().isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute("""
                DELETE FROM acp_sessions
                WHERE is_active = 0 AND updated_at < datetime(?, '-' || ? || ' days')
            """, (cutoff_date, days_old))
            deleted_count = cursor.rowcount
            conn.commit()
            return deleted_count

    def close(self) -> None:
        """Close all database connections."""
        if hasattr(self._local, 'connection'):
            self._local.connection.close()