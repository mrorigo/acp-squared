"""
Tests for ACP² stateful agent functionality.

Tests session persistence, message history, and session management features.
"""

import pytest
import asyncio
import json
import tempfile
from pathlib import Path

from src.acp2_proxy.database import SessionDatabase, ACPSession
from src.acp2_proxy.session_manager import SessionManager
from src.acp2_proxy.models import Message, MessagePart


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name

    yield db_path

    # Cleanup
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
async def database(temp_db):
    """Create database instance for testing."""
    db = SessionDatabase(temp_db)
    yield db
    db.close()


@pytest.fixture
def agent_config():
    """Sample agent configuration for testing."""
    return {
        "test-agent": {
            "name": "test-agent",
            "command": ["python", "tests/dummy_agent.py"],
            "description": "Test agent for stateful functionality"
        }
    }


@pytest.fixture
async def session_manager(database, agent_config):
    """Create session manager for testing."""
    return SessionManager(database, agent_config)


class TestSessionDatabase:
    """Test the database layer functionality."""

    @pytest.mark.anyio
    async def test_create_and_get_session(self, database):
        """Test creating and retrieving ACP sessions."""
        # Create a session
        acp_session = await database.create_acp_session(
            acp_session_id="test_session_123",
            agent="test-agent",
            cwd="/test/dir",
            zed_session_id="zed_456"
        )

        assert acp_session.acp_session_id == "test_session_123"
        assert acp_session.agent_name == "test-agent"
        assert acp_session.zed_session_id == "zed_456"
        assert acp_session.working_directory == "/test/dir"

        # Retrieve the session
        retrieved = await database.get_acp_session("test_session_123")
        assert retrieved is not None
        assert retrieved.acp_session_id == "test_session_123"
        assert retrieved.agent_name == "test-agent"

        # Test non-existent session
        not_found = await database.get_acp_session("non_existent")
        assert not_found is None

    @pytest.mark.anyio
    async def test_session_history(self, database):
        """Test message history storage and retrieval."""
        # Create session first
        await database.create_acp_session(
            acp_session_id="history_test_session",
            agent="test-agent",
            cwd="/test",
            zed_session_id="zed_history"
        )

        # Create test messages
        user_message = Message(
            role="user",
            content=[MessagePart(type="text", text="Hello, agent!")]
        )

        assistant_message = Message(
            role="assistant",
            content=[MessagePart(type="text", text="Hello, human!")]
        )

        # Store messages
        await database.append_message_history(
            acp_session_id="history_test_session",
            run_id="run_001",
            message=user_message,
            sequence_number=0
        )

        await database.append_message_history(
            acp_session_id="history_test_session",
            run_id="run_001",
            message=assistant_message,
            sequence_number=1
        )

        # Retrieve history
        history = await database.get_session_history("history_test_session")
        assert len(history) == 2

        # Check message order and content
        assert history[0].message_role == "user"
        assert history[0].run_id == "run_001"
        assert history[0].sequence_number == 0

        assert history[1].message_role == "assistant"
        assert history[1].run_id == "run_001"
        assert history[1].sequence_number == 1

    @pytest.mark.anyio
    async def test_list_sessions(self, database):
        """Test listing sessions with filtering."""
        # Create test sessions
        await database.create_acp_session("session_1", "agent_a", "/dir1", "zed_1")
        await database.create_acp_session("session_2", "agent_b", "/dir2", "zed_2")
        await database.create_acp_session("session_3", "agent_a", "/dir3", "zed_3")

        # List all sessions
        all_sessions = await database.list_acp_sessions()
        assert len(all_sessions) == 3

        # Filter by agent
        agent_a_sessions = await database.list_acp_sessions(agent_name="agent_a")
        assert len(agent_a_sessions) == 2
        assert all(s.agent_name == "agent_a" for s in agent_a_sessions)

        # Test active only (default)
        active_sessions = await database.list_acp_sessions(active_only=True)
        assert len(active_sessions) == 3  # All are active by default

    @pytest.mark.anyio
    async def test_delete_session(self, database):
        """Test session deletion."""
        # Create and delete a session
        await database.create_acp_session("delete_test", "test", "/tmp", "zed_del")

        # Verify it exists
        session = await database.get_acp_session("delete_test")
        assert session is not None

        # Delete it
        deleted = await database.delete_acp_session("delete_test")
        assert deleted is True

        # Verify it's gone
        session = await database.get_acp_session("delete_test")
        assert session is None

        # Test deleting non-existent session
        not_deleted = await database.delete_acp_session("non_existent")
        assert not_deleted is False


class TestSessionManager:
    """Test the session manager functionality."""

    @pytest.mark.anyio
    async def test_get_or_create_session(self, session_manager):
        """Test session creation and retrieval."""
        # Create new session
        active_session = await session_manager.get_or_create_session(
            acp_session_id="manager_test_session",
            agent="test-agent",
            cwd="/test/cwd"
        )

        assert active_session.acp_session.acp_session_id == "manager_test_session"
        assert active_session.acp_session.agent_name == "test-agent"
        assert active_session.acp_session.working_directory == "/test/cwd"

        # Retrieve existing session
        same_session = await session_manager.get_or_create_session(
            acp_session_id="manager_test_session",
            agent="test-agent",
            cwd="/test/cwd"
        )

        assert same_session.acp_session.acp_session_id == "manager_test_session"

    @pytest.mark.anyio
    async def test_session_history_through_manager(self, session_manager):
        """Test message history through session manager."""
        # Create session
        active_session = await session_manager.get_or_create_session(
            "history_manager_test",
            "test-agent",
            "/test"
        )

        # Create test message
        test_message = Message(
            role="user",
            content=[MessagePart(type="text", text="Test message")]
        )

        # Append to history
        await session_manager.append_message_to_history(
            acp_session_id="history_manager_test",
            run_id="test_run",
            message=test_message,
            sequence_number=0
        )

        # Retrieve history
        history = await session_manager.get_session_history("history_manager_test")
        assert len(history) == 1
        assert history[0].message_role == "user"
        assert history[0].run_id == "test_run"

    @pytest.mark.anyio
    async def test_session_activity_update(self, session_manager):
        """Test session activity tracking."""
        # Create session
        active_session = await session_manager.get_or_create_session(
            "activity_test",
            "test-agent",
            "/test"
        )

        # Update activity
        await session_manager.update_session_activity("activity_test", "run_123")

        # Verify session was updated
        updated_session = await session_manager.get_acp_session("activity_test")
        assert updated_session is not None
        assert updated_session.last_run_id == "run_123"

    @pytest.mark.anyio
    async def test_session_cleanup(self, session_manager):
        """Test session deletion through manager."""
        # Create session
        await session_manager.get_or_create_session("cleanup_test", "test-agent", "/test")

        # Verify it exists
        session = await session_manager.get_acp_session("cleanup_test")
        assert session is not None

        # Delete through manager
        deleted = await session_manager.delete_acp_session("cleanup_test")
        assert deleted is True

        # Verify it's gone
        session = await session_manager.get_acp_session("cleanup_test")
        assert session is None

    @pytest.mark.anyio
    async def test_health_check(self, session_manager):
        """Test session manager health check."""
        health = await session_manager.health_check()

        assert health["status"] == "healthy"
        assert "total_sessions" in health
        assert "active_sessions" in health
        assert "database_path" in health


class TestStatefulIntegration:
    """Integration tests for stateful agent functionality."""

    @pytest.mark.anyio
    async def test_complete_stateful_workflow(self, session_manager):
        """Test a complete stateful conversation workflow."""
        session_id = "integration_test_session"

        # Step 1: Create session
        active_session = await session_manager.get_or_create_session(
            session_id, "test-agent", "/test/workdir"
        )

        # Step 2: Simulate conversation
        messages = [
            ("Hello, agent!", "Hello! How can I help?"),
            ("What's the weather?", "I don't have access to weather data."),
            ("Tell me a joke.", "Why did the chicken cross the road?")
        ]

        for i, (user_msg, assistant_response) in enumerate(messages):
            run_id = f"run_{i}"

            # Store user message
            user_message = Message(
                role="user",
                content=[MessagePart(type="text", text=user_msg)]
            )

            # Store assistant response
            assistant_message = Message(
                role="assistant",
                content=[MessagePart(type="text", text=assistant_response)]
            )

            # Append both messages to history
            await session_manager.append_message_to_history(
                session_id, run_id, user_message, 0
            )
            await session_manager.append_message_to_history(
                session_id, run_id, assistant_message, 1
            )

            # Update activity
            await session_manager.update_session_activity(session_id, run_id)

        # Step 3: Verify complete history
        history = await session_manager.get_session_history(session_id)
        assert len(history) == 6  # 3 conversations × 2 messages each

        # Step 4: Verify session metadata
        session = await session_manager.get_acp_session(session_id)
        assert session.last_run_id == "run_2"
        assert session.agent_name == "test-agent"

        # Step 5: List all sessions
        sessions = await session_manager.list_acp_sessions()
        session_ids = [s.acp_session_id for s in sessions]
        assert session_id in session_ids

        # Step 6: Cleanup
        deleted = await session_manager.delete_acp_session(session_id)
        assert deleted is True

        # Verify cleanup
        sessions_after = await session_manager.list_acp_sessions()
        session_ids_after = [s.acp_session_id for s in sessions_after]
        assert session_id not in session_ids_after


if __name__ == "__main__":
    pytest.main([__file__, "-v"])