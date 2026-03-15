import pytest
import json
import tempfile
import os
import sys

# Set test environment variables before importing
os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")
os.environ.setdefault("DEFAULT_MODEL", "claude-opus-4-6")
os.environ.setdefault("PERMISSION_MODE", "bypassPermissions")

# Add parent directory to path to import session_store
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_config import DEFAULT_MODEL, PERMISSION_MODE
from session_store import SessionStore


@pytest.fixture
def temp_store():
    """Create a temporary session store for testing"""
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)

    # Create a SessionStore with custom path
    store = SessionStore()
    store.SESSIONS_FILE = path
    store._data = {}
    store._save()

    yield store

    # Cleanup
    if os.path.exists(path):
        os.unlink(path)


def test_get_current_with_chat_id_private(temp_store):
    """Test getting current session for private chat"""
    user_id = "user_123"
    chat_id = "user_123"  # Private chat: chat_id = user_id

    # Should return default session for new user
    session = temp_store.get_current(user_id, chat_id)
    assert session.model == DEFAULT_MODEL
    assert session.permission_mode == PERMISSION_MODE


def test_get_current_with_chat_id_group(temp_store):
    """Test getting current session for group chat"""
    user_id = "user_123"
    chat_id = "group_456"

    # Should return default session for new group
    session = temp_store.get_current(user_id, chat_id)
    assert session.model == DEFAULT_MODEL
    assert session.permission_mode == PERMISSION_MODE


def test_session_isolation_between_chats(temp_store):
    """Test that private and group sessions are isolated"""
    user_id = "user_123"
    private_chat_id = "user_123"
    group_chat_id = "group_456"

    # Set different models for private and group
    temp_store.set_model(user_id, private_chat_id, "claude-sonnet-4-6")
    temp_store.set_model(user_id, group_chat_id, "claude-haiku-4-5-20251001")

    # Verify isolation
    private_session = temp_store.get_current(user_id, private_chat_id)
    group_session = temp_store.get_current(user_id, group_chat_id)

    assert private_session.model == "claude-sonnet-4-6"
    assert group_session.model == "claude-haiku-4-5-20251001"


def test_set_model_with_chat_id(temp_store):
    """Test setting model for specific chat"""
    user_id = "user_123"
    chat_id = "group_456"

    temp_store.set_model(user_id, chat_id, "claude-sonnet-4-6")

    session = temp_store.get_current(user_id, chat_id)
    assert session.model == "claude-sonnet-4-6"


def test_set_permission_mode_with_chat_id(temp_store):
    user_id = "user_123"
    chat_id = "group_456"

    temp_store.set_permission_mode(user_id, chat_id, "plan")
    session = temp_store.get_current(user_id, chat_id)
    assert session.permission_mode == "plan"


def test_set_cwd_with_chat_id(temp_store):
    user_id = "user_123"
    chat_id = "group_456"

    temp_store.set_cwd(user_id, chat_id, "/tmp")
    session = temp_store.get_current(user_id, chat_id)
    assert session.cwd == "/tmp"


def test_new_session_with_chat_id(temp_store):
    user_id = "user_123"
    chat_id = "group_456"

    # Create initial session
    temp_store.set_model(user_id, chat_id, "claude-sonnet-4-6")

    # Start new session
    old_title = temp_store.new_session(user_id, chat_id)

    # Verify new session is clean
    session = temp_store.get_current(user_id, chat_id)
    assert session.session_id is None


def test_list_sessions_with_chat_id(temp_store):
    user_id = "user_123"
    chat_id = "group_456"

    # Initially empty
    sessions = temp_store.list_sessions(user_id, chat_id)
    assert len(sessions) == 0

    # Create and archive a session with a session_id
    temp_store.set_model(user_id, chat_id, "claude-sonnet-4-6")
    # Manually set a session_id to simulate a real session
    raw = temp_store.get_current_raw(user_id, chat_id)
    raw["session_id"] = "test_session_123"
    temp_store._save()

    # Now create new session, which should archive the old one
    temp_store.new_session(user_id, chat_id)

    # Should have one archived session
    sessions = temp_store.list_sessions(user_id, chat_id)
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "test_session_123"
