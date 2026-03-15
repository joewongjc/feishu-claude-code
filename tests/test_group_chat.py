"""
Integration tests for group chat functionality.
Tests message handling, session isolation, and chat detection.
"""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock

# Set environment variables before importing modules
os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")
os.environ.setdefault("FEISHU_VERIFICATION_TOKEN", "test_token")
os.environ.setdefault("FEISHU_ENCRYPT_KEY", "test_key")

from main import extract_chat_info
from session_store import SessionStore


@pytest.fixture
def session_store():
    """Create a session store for testing"""
    return SessionStore()


# ── Test extract_chat_info ──────────────────────────────────

def test_extract_chat_info_private_chat():
    """Test extracting chat info from private chat message"""
    mock_event = MagicMock()
    mock_event.event.sender.sender_id.open_id = "user_123"
    mock_event.event.message.chat_type = "p2p"
    mock_event.event.message.chat_id = "user_123"

    user_id, chat_id, is_group = extract_chat_info(mock_event)

    assert user_id == "user_123"
    assert chat_id == "user_123"
    assert is_group is False


def test_extract_chat_info_group_chat():
    """Test extracting chat info from group chat message"""
    mock_event = MagicMock()
    mock_event.event.sender.sender_id.open_id = "user_456"
    mock_event.event.message.chat_type = "group"
    mock_event.event.message.chat_id = "group_789"

    user_id, chat_id, is_group = extract_chat_info(mock_event)

    assert user_id == "user_456"
    assert chat_id == "group_789"
    assert is_group is True


# ── Test session isolation ──────────────────────────────────

def test_session_isolation_private_vs_group(session_store):
    """Test that private and group chats have isolated sessions"""
    user_id = "user_123"
    private_chat_id = user_id
    group_chat_id = "group_456"

    # Get session for private chat
    private_session = session_store.get_current(user_id, private_chat_id)
    private_session.model = "claude-opus"
    session_store.set_model(user_id, private_chat_id, "claude-opus")

    # Get session for group chat
    group_session = session_store.get_current(user_id, group_chat_id)
    group_session.model = "claude-sonnet"
    session_store.set_model(user_id, group_chat_id, "claude-sonnet")

    # Verify isolation
    private_check = session_store.get_current(user_id, private_chat_id)
    group_check = session_store.get_current(user_id, group_chat_id)

    assert private_check.model == "claude-opus"
    assert group_check.model == "claude-sonnet"


def test_multiple_groups_isolation(session_store):
    """Test that multiple groups have independent sessions"""
    user_id = "user_123"
    group1_id = "group_001"
    group2_id = "group_002"

    # Set different models for each group
    session_store.set_model(user_id, group1_id, "claude-opus")
    session_store.set_model(user_id, group2_id, "claude-sonnet")

    # Verify each group has its own model
    group1_session = session_store.get_current(user_id, group1_id)
    group2_session = session_store.get_current(user_id, group2_id)

    assert group1_session.model == "claude-opus"
    assert group2_session.model == "claude-sonnet"


def test_multiple_users_in_same_group(session_store):
    """Test that different users in same group have separate sessions"""
    group_id = "group_123"
    user1_id = "user_001"
    user2_id = "user_002"

    # Set different models for each user in same group
    session_store.set_model(user1_id, group_id, "claude-opus")
    session_store.set_model(user2_id, group_id, "claude-sonnet")

    # Verify each user has their own session
    user1_session = session_store.get_current(user1_id, group_id)
    user2_session = session_store.get_current(user2_id, group_id)

    assert user1_session.model == "claude-opus"
    assert user2_session.model == "claude-sonnet"


# ── Test session operations with chat_id ──────────────────

def test_set_permission_mode_with_chat_id(session_store):
    """Test setting permission mode for specific chat"""
    user_id = "user_123"
    chat_id = "group_456"

    session_store.set_permission_mode(user_id, chat_id, "restricted")
    session = session_store.get_current(user_id, chat_id)

    assert session.permission_mode == "restricted"


def test_set_cwd_with_chat_id(session_store):
    """Test setting working directory for specific chat"""
    user_id = "user_123"
    chat_id = "group_456"

    session_store.set_cwd(user_id, chat_id, "/tmp/work")
    session = session_store.get_current(user_id, chat_id)

    assert session.cwd == "/tmp/work"


def test_new_session_with_chat_id(session_store):
    """Test creating new session for specific chat"""
    user_id = "user_123"
    chat_id = "group_456"

    session_store.new_session(user_id, chat_id)
    session = session_store.get_current(user_id, chat_id)

    assert session.session_id is None  # New session has no ID yet


def test_list_sessions_with_chat_id(session_store):
    """Test listing sessions for specific chat"""
    user_id = "user_list_test"
    chat_id = "group_list_test"

    # Initially empty for this user/chat combo
    sessions = session_store.list_sessions(user_id, chat_id)
    assert isinstance(sessions, list)


def test_resume_session_with_chat_id(session_store):
    """Test resuming session in specific chat"""
    user_id = "user_resume_test"
    chat_id = "group_resume_test"

    # Try to resume non-existent session
    resumed_id, old_title = session_store.resume_session(user_id, chat_id, "1")
    assert resumed_id is None
    assert old_title == ""
