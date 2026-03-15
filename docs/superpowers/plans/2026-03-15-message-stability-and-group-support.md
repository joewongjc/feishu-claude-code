# Message Stability and Group Support Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix message instability by switching to one-time sending, and add group chat support with isolated sessions.

**Architecture:** Refactor session storage to use `(user_id, chat_id)` composite keys, remove streaming patch logic and accumulate full responses before sending, add group chat message detection.

**Tech Stack:** Python 3.11+, lark-oapi (Feishu SDK), asyncio

---

## File Structure

### Modified Files
- `session_store.py` - Add `chat_id` parameter to all methods, implement data migration
- `main.py` - Remove streaming logic, add group chat detection, one-time message sending
- `claude_runner.py` - Change from streaming callbacks to full response accumulation
- `commands.py` - Add `chat_id` parameter to all session queries
- `feishu_client.py` - Simplify, keep only single patch for placeholder replacement

### New Files
- `migrate_sessions.py` - Standalone migration script for existing session data
- `tests/test_session_store.py` - Unit tests for session storage
- `tests/test_group_chat.py` - Integration tests for group chat functionality

---

## Chunk 1: Session Store Refactoring

### Task 1: Add chat_id Support to SessionStore

**Files:**
- Modify: `session_store.py`
- Test: `tests/test_session_store.py` (create)

- [ ] **Step 1: Read current session_store.py implementation**

Read the file to understand current structure:

```bash
cat session_store.py
```

- [ ] **Step 2: Write failing test for get_current with chat_id**

Create `tests/test_session_store.py`:

```python
import pytest
import json
import tempfile
import os
from session_store import SessionStore


@pytest.fixture
def temp_store():
    """Create a temporary session store for testing"""
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    store = SessionStore(path)
    yield store
    os.unlink(path)


def test_get_current_with_chat_id_private(temp_store):
    """Test getting current session for private chat"""
    user_id = "user_123"
    chat_id = "user_123"  # Private chat: chat_id = user_id

    # Should return default session for new user
    session = temp_store.get_current(user_id, chat_id)
    assert session.model == "claude-opus-4-6"
    assert session.permission_mode == "bypassPermissions"


def test_get_current_with_chat_id_group(temp_store):
    """Test getting current session for group chat"""
    user_id = "user_123"
    chat_id = "group_456"

    # Should return default session for new group
    session = temp_store.get_current(user_id, chat_id)
    assert session.model == "claude-opus-4-6"
    assert session.permission_mode == "bypassPermissions"


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
```

- [ ] **Step 3: Run test to verify it fails**

```bash
pytest tests/test_session_store.py -v
```

Expected: FAIL with "TypeError: get_current() takes 2 positional arguments but 3 were given"

- [ ] **Step 4: Add chat_id parameter to get_current method**

Modify `session_store.py`:

```python
def get_current(self, user_id: str, chat_id: str) -> SessionConfig:
    """Get current session config for a specific chat"""
    raw = self.get_current_raw(user_id, chat_id)
    return SessionConfig(
        session_id=raw.get("session_id", ""),
        model=raw.get("model", "claude-opus-4-6"),
        permission_mode=raw.get("permission_mode", "bypassPermissions"),
        cwd=raw.get("cwd", os.path.expanduser("~")),
    )
```

- [ ] **Step 5: Add chat_id parameter to get_current_raw method**

Modify `session_store.py`:

```python
def get_current_raw(self, user_id: str, chat_id: str) -> dict:
    """Get raw current session data for a specific chat"""
    self._ensure_user(user_id)

    # Normalize chat_id: private chat uses "private" key
    chat_key = "private" if chat_id == user_id else chat_id

    # Ensure chat exists
    if chat_key not in self.data[user_id]:
        self.data[user_id][chat_key] = {
            "current_session": {},
            "sessions": []
        }
        self._save()

    return self.data[user_id][chat_key].get("current_session", {})
```

- [ ] **Step 6: Update _ensure_user to use new structure**

Modify `session_store.py`:

```python
def _ensure_user(self, user_id: str):
    """Ensure user exists in data with new structure"""
    if user_id not in self.data:
        self.data[user_id] = {}
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
pytest tests/test_session_store.py::test_get_current_with_chat_id_private -v
pytest tests/test_session_store.py::test_get_current_with_chat_id_group -v
pytest tests/test_session_store.py::test_session_isolation_between_chats -v
```

Expected: All PASS

- [ ] **Step 8: Commit session store chat_id support**

```bash
git add session_store.py tests/test_session_store.py
git commit -m "feat(session): add chat_id parameter for session isolation

- Add chat_id to get_current() and get_current_raw()
- Use 'private' key for private chats (chat_id == user_id)
- Use chat_id directly for group chats
- Add tests for session isolation

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Update All SessionStore Methods with chat_id

**Files:**
- Modify: `session_store.py`
- Test: `tests/test_session_store.py`

- [ ] **Step 1: Write failing tests for set_model with chat_id**

Add to `tests/test_session_store.py`:

```python
def test_set_model_with_chat_id(temp_store):
    """Test setting model for specific chat"""
    user_id = "user_123"
    chat_id = "group_456"

    temp_store.set_model(user_id, chat_id, "claude-sonnet-4-6")

    session = temp_store.get_current(user_id, chat_id)
    assert session.model == "claude-sonnet-4-6"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_session_store.py::test_set_model_with_chat_id -v
```

Expected: FAIL

- [ ] **Step 3: Update set_model method**

Modify `session_store.py`:

```python
def set_model(self, user_id: str, chat_id: str, model: str):
    """Set model for a specific chat"""
    self._ensure_user(user_id)
    chat_key = "private" if chat_id == user_id else chat_id

    if chat_key not in self.data[user_id]:
        self.data[user_id][chat_key] = {"current_session": {}, "sessions": []}

    if "current_session" not in self.data[user_id][chat_key]:
        self.data[user_id][chat_key]["current_session"] = {}

    self.data[user_id][chat_key]["current_session"]["model"] = model
    self._save()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_session_store.py::test_set_model_with_chat_id -v
```

Expected: PASS

- [ ] **Step 5: Update set_permission_mode method**

Modify `session_store.py`:

```python
def set_permission_mode(self, user_id: str, chat_id: str, mode: str):
    """Set permission mode for a specific chat"""
    self._ensure_user(user_id)
    chat_key = "private" if chat_id == user_id else chat_id

    if chat_key not in self.data[user_id]:
        self.data[user_id][chat_key] = {"current_session": {}, "sessions": []}

    if "current_session" not in self.data[user_id][chat_key]:
        self.data[user_id][chat_key]["current_session"] = {}

    self.data[user_id][chat_key]["current_session"]["permission_mode"] = mode
    self._save()
```

- [ ] **Step 6: Update set_cwd method**

Modify `session_store.py`:

```python
def set_cwd(self, user_id: str, chat_id: str, cwd: str):
    """Set working directory for a specific chat"""
    self._ensure_user(user_id)
    chat_key = "private" if chat_id == user_id else chat_id

    if chat_key not in self.data[user_id]:
        self.data[user_id][chat_key] = {"current_session": {}, "sessions": []}

    if "current_session" not in self.data[user_id][chat_key]:
        self.data[user_id][chat_key]["current_session"] = {}

    self.data[user_id][chat_key]["current_session"]["cwd"] = cwd
    self._save()
```

- [ ] **Step 7: Update new_session method**

Modify `session_store.py`:

```python
def new_session(self, user_id: str, chat_id: str) -> Optional[str]:
    """Start a new session for a specific chat, return old session title"""
    self._ensure_user(user_id)
    chat_key = "private" if chat_id == user_id else chat_id

    if chat_key not in self.data[user_id]:
        self.data[user_id][chat_key] = {"current_session": {}, "sessions": []}

    old_session = self.data[user_id][chat_key].get("current_session", {})
    old_title = None

    if old_session.get("session_id"):
        # Save old session to history
        if "sessions" not in self.data[user_id][chat_key]:
            self.data[user_id][chat_key]["sessions"] = []

        self.data[user_id][chat_key]["sessions"].append(old_session)
        old_title = old_session.get("preview", "")[:50]

    # Clear current session
    self.data[user_id][chat_key]["current_session"] = {}
    self._save()

    return old_title
```

- [ ] **Step 8: Update resume_session method**

Modify `session_store.py`:

```python
def resume_session(self, user_id: str, chat_id: str, session_id: str) -> tuple[str, Optional[str]]:
    """Resume a session for a specific chat, return (session_id, old_title)"""
    self._ensure_user(user_id)
    chat_key = "private" if chat_id == user_id else chat_id

    if chat_key not in self.data[user_id]:
        return "", None

    # Find session in history
    sessions = self.data[user_id][chat_key].get("sessions", [])
    target_session = None

    for s in sessions:
        if s.get("session_id", "").startswith(session_id):
            target_session = s
            break

    if not target_session:
        return "", None

    # Save current session to history
    old_session = self.data[user_id][chat_key].get("current_session", {})
    old_title = None

    if old_session.get("session_id"):
        self.data[user_id][chat_key]["sessions"].append(old_session)
        old_title = old_session.get("preview", "")[:50]

    # Set target as current
    self.data[user_id][chat_key]["current_session"] = target_session

    # Remove from history
    self.data[user_id][chat_key]["sessions"] = [
        s for s in sessions if s.get("session_id") != target_session.get("session_id")
    ]

    self._save()
    return target_session.get("session_id", ""), old_title
```

- [ ] **Step 9: Update list_sessions method**

Modify `session_store.py`:

```python
def list_sessions(self, user_id: str, chat_id: str) -> list[dict]:
    """List all sessions for a specific chat"""
    self._ensure_user(user_id)
    chat_key = "private" if chat_id == user_id else chat_id

    if chat_key not in self.data[user_id]:
        return []

    return self.data[user_id][chat_key].get("sessions", [])
```

- [ ] **Step 10: Write comprehensive tests for all updated methods**

Add to `tests/test_session_store.py`:

```python
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
    assert session.session_id == ""


def test_list_sessions_with_chat_id(temp_store):
    user_id = "user_123"
    chat_id = "group_456"

    # Initially empty
    sessions = temp_store.list_sessions(user_id, chat_id)
    assert len(sessions) == 0

    # Create and archive a session
    temp_store.set_model(user_id, chat_id, "claude-sonnet-4-6")
    temp_store.new_session(user_id, chat_id)

    # Should have one archived session
    sessions = temp_store.list_sessions(user_id, chat_id)
    assert len(sessions) == 1
```

- [ ] **Step 11: Run all tests**

```bash
pytest tests/test_session_store.py -v
```

Expected: All PASS

- [ ] **Step 12: Commit updated SessionStore methods**

```bash
git add session_store.py tests/test_session_store.py
git commit -m "feat(session): update all methods to support chat_id

- Update set_model, set_permission_mode, set_cwd
- Update new_session, resume_session, list_sessions
- All methods now use (user_id, chat_id) composite key
- Add comprehensive tests

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Chunk 2: Data Migration

### Task 3: Create Migration Script

**Files:**
- Create: `migrate_sessions.py`
- Test: Manual testing with backup

- [ ] **Step 1: Create migration script with backup logic**

Create `migrate_sessions.py`:

```python
#!/usr/bin/env python3
"""
Migrate session data from old format to new format.
Old: {user_id: {current_session, sessions}}
New: {user_id: {private: {current_session, sessions}, group_id: {...}}}
"""

import json
import os
import shutil
from datetime import datetime


def migrate_sessions(sessions_path: str):
    """Migrate sessions.json to new format"""
    
    # Check if file exists
    if not os.path.exists(sessions_path):
        print(f"❌ File not found: {sessions_path}")
        return False
    
    # Load old data
    print(f"📖 Loading {sessions_path}...")
    with open(sessions_path, 'r', encoding='utf-8') as f:
        old_data = json.load(f)
    
    print(f"✅ Loaded {len(old_data)} users")
    
    # Check if already migrated
    if old_data and isinstance(list(old_data.values())[0], dict):
        first_user_data = list(old_data.values())[0]
        if "private" in first_user_data or any(k.startswith("group_") for k in first_user_data.keys()):
            print("⚠️  Data appears to be already migrated (has 'private' or 'group_' keys)")
            response = input("Continue anyway? (y/N): ")
            if response.lower() != 'y':
                return False
    
    # Backup original file
    backup_path = f"{sessions_path}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print(f"💾 Creating backup: {backup_path}")
    shutil.copy2(sessions_path, backup_path)
    
    # Migrate data
    new_data = {}
    for user_id, user_sessions in old_data.items():
        # Check if user_sessions has the old structure
        if "current_session" in user_sessions or "sessions" in user_sessions:
            # Old format: move everything to "private" key
            new_data[user_id] = {
                "private": user_sessions
            }
        else:
            # Already new format or empty
            new_data[user_id] = user_sessions
    
    # Validate migration
    print("🔍 Validating migration...")
    assert len(new_data) == len(old_data), "User count mismatch"
    
    for user_id in old_data:
        if "current_session" in old_data[user_id] or "sessions" in old_data[user_id]:
            assert "private" in new_data[user_id], f"Missing 'private' key for {user_id}"
            assert new_data[user_id]["private"] == old_data[user_id], f"Data mismatch for {user_id}"
    
    print("✅ Validation passed")
    
    # Write new data
    print(f"💾 Writing migrated data to {sessions_path}...")
    with open(sessions_path, 'w', encoding='utf-8') as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)
    
    print(f"✅ Migration complete!")
    print(f"📊 Migrated {len(new_data)} users")
    print(f"💾 Backup saved to: {backup_path}")
    print(f"\n⚠️  If you encounter issues, restore with:")
    print(f"   cp {backup_path} {sessions_path}")
    
    return True


if __name__ == "__main__":
    import sys
    
    # Default path
    default_path = os.path.expanduser("~/.feishu-claude/sessions.json")
    
    if len(sys.argv) > 1:
        sessions_path = sys.argv[1]
    else:
        sessions_path = default_path
    
    print("🚀 Session Data Migration Tool")
    print(f"📁 Target: {sessions_path}\n")
    
    success = migrate_sessions(sessions_path)
    
    if success:
        print("\n✅ Migration successful!")
        sys.exit(0)
    else:
        print("\n❌ Migration failed or cancelled")
        sys.exit(1)
```

- [ ] **Step 2: Make migration script executable**

```bash
chmod +x migrate_sessions.py
```

- [ ] **Step 3: Test migration with sample data**

Create test data:

```bash
mkdir -p /tmp/test-migration
cat > /tmp/test-migration/sessions.json << 'TESTDATA'
{
  "user_123": {
    "current_session": {
      "session_id": "abc123",
      "model": "claude-opus-4-6",
      "permission_mode": "bypassPermissions",
      "cwd": "/home/user"
    },
    "sessions": [
      {
        "session_id": "old123",
        "preview": "Previous session"
      }
    ]
  },
  "user_456": {
    "current_session": {},
    "sessions": []
  }
}
TESTDATA
```

Run migration:

```bash
python migrate_sessions.py /tmp/test-migration/sessions.json
```

Expected output:
```
🚀 Session Data Migration Tool
📁 Target: /tmp/test-migration/sessions.json

📖 Loading /tmp/test-migration/sessions.json...
✅ Loaded 2 users
💾 Creating backup: /tmp/test-migration/sessions.json.backup.YYYYMMDD_HHMMSS
🔍 Validating migration...
✅ Validation passed
💾 Writing migrated data to /tmp/test-migration/sessions.json...
✅ Migration complete!
📊 Migrated 2 users
```

- [ ] **Step 4: Verify migrated data structure**

```bash
cat /tmp/test-migration/sessions.json
```

Expected structure:
```json
{
  "user_123": {
    "private": {
      "current_session": {
        "session_id": "abc123",
        "model": "claude-opus-4-6",
        "permission_mode": "bypassPermissions",
        "cwd": "/home/user"
      },
      "sessions": [
        {
          "session_id": "old123",
          "preview": "Previous session"
        }
      ]
    }
  },
  "user_456": {
    "private": {
      "current_session": {},
      "sessions": []
    }
  }
}
```

- [ ] **Step 5: Test rollback**

```bash
# Find backup file
BACKUP=$(ls -t /tmp/test-migration/sessions.json.backup.* | head -1)
# Restore
cp "$BACKUP" /tmp/test-migration/sessions.json
# Verify
cat /tmp/test-migration/sessions.json
```

Expected: Original data restored

- [ ] **Step 6: Clean up test data**

```bash
rm -rf /tmp/test-migration
```

- [ ] **Step 7: Commit migration script**

```bash
git add migrate_sessions.py
git commit -m "feat(migration): add session data migration script

- Migrate from old format to new (user_id, chat_id) structure
- Automatic backup before migration
- Validation and rollback support
- Tested with sample data

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 4: Add Migration Instructions to README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add migration section to README**

Add to `README.md` before "Usage" section:

```markdown
## Migration (for existing users)

If you're upgrading from a previous version, you need to migrate your session data:

```bash
# Backup your data first (optional, script does this automatically)
cp ~/.feishu-claude/sessions.json ~/.feishu-claude/sessions.json.manual-backup

# Run migration
python migrate_sessions.py

# If something goes wrong, restore from backup:
cp ~/.feishu-claude/sessions.json.backup.YYYYMMDD_HHMMSS ~/.feishu-claude/sessions.json
```

The migration script:
- Automatically backs up your data
- Migrates to new format (adds group chat support)
- Validates the migration
- Provides rollback instructions if needed

**Note**: After migration, your existing private chat sessions will continue to work normally.
```

- [ ] **Step 2: Commit README update**

```bash
git add README.md
git commit -m "docs: add migration instructions for existing users

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Chunk 3: Main Program Refactoring

### Task 5: Add Group Chat Detection

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Read current main.py to understand message handling**

```bash
grep -n "def.*message" main.py | head -20
```

- [ ] **Step 2: Add chat_id extraction function**

Add to `main.py` after imports:

```python
def extract_chat_info(event_dict: dict) -> tuple[str, str, bool]:
    """
    Extract user_id, chat_id, and is_group from message event.
    
    Returns:
        (user_id, chat_id, is_group)
        - For private chat: chat_id = user_id
        - For group chat: chat_id = group's chat_id
    """
    sender = event_dict.get("sender", {})
    user_id = sender.get("sender_id", {}).get("open_id", "")
    
    # Check message type to determine if it's a group chat
    message = event_dict.get("message", {})
    chat_type = message.get("chat_type", "")
    chat_id_raw = message.get("chat_id", "")
    
    # chat_type: "p2p" for private, "group" for group
    is_group = (chat_type == "group")
    
    if is_group:
        chat_id = chat_id_raw
    else:
        # Private chat: use user_id as chat_id
        chat_id = user_id
    
    return user_id, chat_id, is_group
```

- [ ] **Step 3: Write test for extract_chat_info**

Create `tests/test_main.py`:

```python
import pytest
from main import extract_chat_info


def test_extract_chat_info_private():
    """Test extracting chat info from private message"""
    event = {
        "sender": {
            "sender_id": {
                "open_id": "user_123"
            }
        },
        "message": {
            "chat_type": "p2p",
            "chat_id": "user_123"
        }
    }
    
    user_id, chat_id, is_group = extract_chat_info(event)
    
    assert user_id == "user_123"
    assert chat_id == "user_123"
    assert is_group == False


def test_extract_chat_info_group():
    """Test extracting chat info from group message"""
    event = {
        "sender": {
            "sender_id": {
                "open_id": "user_123"
            }
        },
        "message": {
            "chat_type": "group",
            "chat_id": "oc_abc123"
        }
    }
    
    user_id, chat_id, is_group = extract_chat_info(event)
    
    assert user_id == "user_123"
    assert chat_id == "oc_abc123"
    assert is_group == True
```

- [ ] **Step 4: Run test**

```bash
pytest tests/test_main.py -v
```

Expected: All PASS

- [ ] **Step 5: Update message handler to use extract_chat_info**

Find the message handler function in `main.py` and update it to extract chat_id:

```python
# Before: user_id = ...
# After:
user_id, chat_id, is_group = extract_chat_info(event_dict)
```

Update all session store calls to include chat_id:

```python
# Before: store.get_current(user_id)
# After: store.get_current(user_id, chat_id)
```

- [ ] **Step 6: Commit group chat detection**

```bash
git add main.py tests/test_main.py
git commit -m "feat(main): add group chat detection

- Add extract_chat_info() to identify private vs group chats
- Extract chat_id from message events
- Update session store calls to include chat_id
- Add tests for chat info extraction

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 6: Remove Streaming Logic and Implement One-Time Sending

**Files:**
- Modify: `main.py`
- Modify: `claude_runner.py`

- [ ] **Step 1: Read current streaming implementation**

```bash
grep -n "STREAM_CHUNK_SIZE\|patch\|update_card" main.py
```

- [ ] **Step 2: Modify claude_runner.py to accumulate full response**

Find `run_claude()` function in `claude_runner.py` and modify it:

```python
def run_claude(
    prompt: str,
    session_id: str = "",
    model: str = "claude-opus-4-6",
    permission_mode: str = "bypassPermissions",
    cwd: str = "~",
    timeout: int = 120,
) -> tuple[str, str]:
    """
    Run Claude CLI and return full response.
    
    Returns:
        (full_response, session_id)
    """
    cmd = [CLAUDE_CLI, "chat"]
    
    if session_id:
        cmd.extend(["--session", session_id])
    
    cmd.extend([
        "--model", model,
        "--permission-mode", permission_mode,
        "--cwd", os.path.expanduser(cwd),
        "--output-format", "stream-json",
    ])
    
    # Accumulate full response
    full_response = []
    extracted_session_id = session_id
    
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        
        # Send prompt
        proc.stdin.write(prompt + "\n")
        proc.stdin.close()
        
        # Read stream-json output
        for line in proc.stdout:
            if not line.strip():
                continue
            
            try:
                data = json.loads(line)
                
                # Extract text chunks
                if data.get("type") == "text":
                    full_response.append(data.get("text", ""))
                
                # Extract session_id
                if data.get("type") == "session" and data.get("session_id"):
                    extracted_session_id = data["session_id"]
                    
            except json.JSONDecodeError:
                continue
        
        proc.wait(timeout=timeout)
        
    except subprocess.TimeoutExpired:
        proc.kill()
        return "⏱️ 回复超时（120秒），请重试", extracted_session_id
    except Exception as e:
        return f"❌ 调用 Claude 失败：{e}", extracted_session_id
    
    return "".join(full_response), extracted_session_id
```

- [ ] **Step 3: Update main.py to use one-time sending**

Find the message processing function and update it:

```python
async def process_message(user_id: str, chat_id: str, prompt: str, store: SessionStore, feishu: FeishuClient):
    """Process a message and send response"""
    
    # Get current session config
    session = store.get_current(user_id, chat_id)
    
    # Send placeholder card
    try:
        card_msg_id = await feishu.send_card_to_user(
            user_id,
            content="",
            loading=True  # Shows "⏳ 思考中..."
        )
    except Exception as e:
        print(f"[error] Failed to send placeholder card: {e}")
        return
    
    # Call Claude and accumulate full response
    full_response, new_session_id = run_claude(
        prompt=prompt,
        session_id=session.session_id,
        model=session.model,
        permission_mode=session.permission_mode,
        cwd=session.cwd,
        timeout=120,
    )
    
    # Update session_id if changed
    if new_session_id and new_session_id != session.session_id:
        store.get_current_raw(user_id, chat_id)["session_id"] = new_session_id
        store._save()
    
    # Replace placeholder with full response (single patch)
    try:
        await feishu.update_card(card_msg_id, full_response)
    except Exception as e:
        print(f"[error] Failed to update card: {e}")
        # Placeholder card remains, user knows it's processing
```

- [ ] **Step 4: Remove STREAM_CHUNK_SIZE and streaming variables**

```bash
# Find and remove these lines from main.py
grep -n "STREAM_CHUNK_SIZE\|accumulated\|chars_since_push" main.py
```

Remove all streaming-related variables and logic.

- [ ] **Step 5: Test with a simple message**

Start the bot and send a test message in private chat:

```
Hello, can you help me?
```

Expected behavior:
1. Placeholder card appears: "⏳ 思考中..."
2. After Claude responds (5-10 seconds), card updates to full response
3. No intermediate updates

- [ ] **Step 6: Test with a long message**

Send a message that triggers a long response:

```
Write a Python function to calculate fibonacci numbers with memoization
```

Expected behavior:
1. Placeholder card appears
2. Wait 10-20 seconds
3. Card updates to full response with complete code

- [ ] **Step 7: Commit one-time sending implementation**

```bash
git add main.py claude_runner.py
git commit -m "feat(main): implement one-time message sending

- Remove streaming patch logic
- Accumulate full Claude response before sending
- Single patch to replace placeholder card
- Remove STREAM_CHUNK_SIZE and streaming variables
- Tested with short and long messages

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Chunk 4: Commands and Feishu Client Updates

### Task 7: Update Commands to Support chat_id

**Files:**
- Modify: `commands.py`

- [ ] **Step 1: Update handle_command signature**

Modify `commands.py`:

```python
def handle_command(
    cmd: str,
    args: str,
    user_id: str,
    chat_id: str,  # Add chat_id parameter
    store: SessionStore,
) -> Optional[str]:
    """Handle command, return reply text. Returns None if not a bot command."""
    
    if cmd not in BOT_COMMANDS:
        return None
    
    # ... rest of function
```

- [ ] **Step 2: Update all store method calls in handle_command**

Find and update all session store calls:

```python
# Before: store.get_current(user_id)
# After: store.get_current(user_id, chat_id)

# Before: store.new_session(user_id)
# After: store.new_session(user_id, chat_id)

# Before: store.set_model(user_id, model)
# After: store.set_model(user_id, chat_id, model)

# Before: store.set_permission_mode(user_id, mode)
# After: store.set_permission_mode(user_id, chat_id, mode)

# Before: store.set_cwd(user_id, path)
# After: store.set_cwd(user_id, chat_id, path)

# Before: store.resume_session(user_id, args)
# After: store.resume_session(user_id, chat_id, args)
```

- [ ] **Step 3: Update _format_session_list to support chat_id**

Modify `_format_session_list()` function:

```python
def _format_session_list(user_id: str, chat_id: str, store: SessionStore) -> str:
    """Generate session list for current chat"""
    from session_store import _clean_preview

    cur = store.get_current_raw(user_id, chat_id)
    cur_sid = cur.get("session_id")

    cli_all = scan_cli_sessions(30)
    cli_preview_map = {s["session_id"]: s for s in cli_all}

    all_sessions = _build_session_list(user_id, chat_id, store)
    
    # ... rest of function (update to use chat_id)
```

- [ ] **Step 4: Update _build_session_list to support chat_id**

Modify `_build_session_list()` function:

```python
def _build_session_list(user_id: str, chat_id: str, store: SessionStore) -> list[dict]:
    """Build merged, deduplicated, sorted session list for current chat"""
    cur_sid = store.get_current_raw(user_id, chat_id).get("session_id")

    cli_all = scan_cli_sessions(30)
    cli_preview_map = {s["session_id"]: s for s in cli_all}

    feishu_sessions = [
        {**s, "source": "feishu"} for s in store.list_sessions(user_id, chat_id)
    ]
    
    # ... rest of function
```

- [ ] **Step 5: Update main.py to pass chat_id to handle_command**

In `main.py`, find where `handle_command()` is called and update:

```python
# Before:
reply = handle_command(cmd, args, user_id, store)

# After:
reply = handle_command(cmd, args, user_id, chat_id, store)
```

- [ ] **Step 6: Test /status command in private chat**

Send in private chat:
```
/status
```

Expected output shows current session info with correct chat context.

- [ ] **Step 7: Test /model command in group chat**

Send in group chat:
```
/model sonnet
```

Expected: Model changed for this group only, not affecting private chat.

- [ ] **Step 8: Verify isolation by checking /status in both chats**

Private chat:
```
/status
```

Group chat:
```
/status
```

Expected: Different model settings.

- [ ] **Step 9: Commit commands update**

```bash
git add commands.py main.py
git commit -m "feat(commands): add chat_id support to all commands

- Update handle_command signature with chat_id
- Update all session store calls to include chat_id
- Update session list functions for chat-specific history
- Tested /status and /model in private and group chats

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 8: Simplify Feishu Client

**Files:**
- Modify: `feishu_client.py`

- [ ] **Step 1: Review current feishu_client.py**

```bash
grep -n "def.*update_card\|def.*patch" feishu_client.py
```

- [ ] **Step 2: Verify update_card is already single-patch**

Check `update_card()` method - it should already do a single patch. If it has any streaming logic, remove it.

Expected implementation:

```python
async def update_card(self, message_id: str, content: str):
    """Update card content with single patch (replaces placeholder)"""
    req = (
        PatchMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            PatchMessageRequestBody.builder()
            .content(_card_json(content, loading=False))
            .build()
        )
        .build()
    )
    resp = await self.client.im.v1.message.apatch(req)
    if not resp.success():
        print(f"[warn] patch card failed: {resp.code} {resp.msg}")
```

- [ ] **Step 3: Remove any unused streaming methods**

Check for any methods that are no longer used (e.g., `update_card_streaming()`, `append_to_card()`, etc.) and remove them.

- [ ] **Step 4: Add retry logic to update_card**

Enhance `update_card()` with retry:

```python
async def update_card(self, message_id: str, content: str, max_retries: int = 3):
    """Update card content with single patch (replaces placeholder)"""
    for attempt in range(max_retries):
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(_card_json(content, loading=False))
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.apatch(req)
        
        if resp.success():
            return True
        
        print(f"[warn] patch card failed (attempt {attempt + 1}/{max_retries}): {resp.code} {resp.msg}")
        
        if attempt < max_retries - 1:
            await asyncio.sleep(1 * (attempt + 1))  # Exponential backoff
    
    return False
```

- [ ] **Step 5: Update send_card_to_user to support retry**

Add retry logic to `send_card_to_user()`:

```python
async def send_card_to_user(self, open_id: str, content: str = "", loading: bool = True, max_retries: int = 3) -> str:
    """Send card message to user, return message_id"""
    for attempt in range(max_retries):
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("interactive")
                .content(_card_json(content, loading=loading))
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.acreate(req)
        
        if resp.success():
            return resp.data.message_id
        
        print(f"[warn] send card failed (attempt {attempt + 1}/{max_retries}): {resp.code} {resp.msg}")
        
        if attempt < max_retries - 1:
            await asyncio.sleep(1 * (attempt + 1))
    
    raise RuntimeError(f"Failed to send card after {max_retries} attempts")
```

- [ ] **Step 6: Test retry logic**

Temporarily disable network to test retry:

```python
# In test environment, simulate network failure
# Verify retry attempts are logged
# Verify exponential backoff works
```

- [ ] **Step 7: Commit feishu client simplification**

```bash
git add feishu_client.py
git commit -m "feat(feishu): simplify client and add retry logic

- Keep only single-patch update_card method
- Remove unused streaming methods
- Add retry logic with exponential backoff
- Max 3 retries for send and update operations

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Chunk 5: Integration Testing

### Task 9: Integration Tests for Group Chat

**Files:**
- Create: `tests/test_group_chat.py`

- [ ] **Step 1: Create integration test file**

Create `tests/test_group_chat.py`:

```python
import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from main import extract_chat_info, process_message
from session_store import SessionStore
import tempfile
import os


@pytest.fixture
def temp_store():
    """Create temporary session store"""
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    store = SessionStore(path)
    yield store
    os.unlink(path)


@pytest.fixture
def mock_feishu():
    """Create mock Feishu client"""
    client = Mock()
    client.send_card_to_user = AsyncMock(return_value="msg_123")
    client.update_card = AsyncMock(return_value=True)
    return client


def test_private_chat_session_isolation(temp_store):
    """Test that private chat has isolated session"""
    user_id = "user_123"
    chat_id = "user_123"  # Private: chat_id = user_id
    
    # Set model for private chat
    temp_store.set_model(user_id, chat_id, "claude-sonnet-4-6")
    
    # Verify
    session = temp_store.get_current(user_id, chat_id)
    assert session.model == "claude-sonnet-4-6"


def test_group_chat_session_isolation(temp_store):
    """Test that group chat has isolated session"""
    user_id = "user_123"
    chat_id = "oc_group456"
    
    # Set model for group chat
    temp_store.set_model(user_id, chat_id, "claude-haiku-4-5-20251001")
    
    # Verify
    session = temp_store.get_current(user_id, chat_id)
    assert session.model == "claude-haiku-4-5-20251001"


def test_private_and_group_isolation(temp_store):
    """Test that private and group chats don't interfere"""
    user_id = "user_123"
    private_chat_id = "user_123"
    group_chat_id = "oc_group456"
    
    # Set different models
    temp_store.set_model(user_id, private_chat_id, "claude-opus-4-6")
    temp_store.set_model(user_id, group_chat_id, "claude-sonnet-4-6")
    
    # Verify isolation
    private_session = temp_store.get_current(user_id, private_chat_id)
    group_session = temp_store.get_current(user_id, group_chat_id)
    
    assert private_session.model == "claude-opus-4-6"
    assert group_session.model == "claude-sonnet-4-6"


def test_multiple_groups_isolation(temp_store):
    """Test that multiple groups have isolated sessions"""
    user_id = "user_123"
    group1_id = "oc_group456"
    group2_id = "oc_group789"
    
    # Set different models for each group
    temp_store.set_model(user_id, group1_id, "claude-opus-4-6")
    temp_store.set_model(user_id, group2_id, "claude-sonnet-4-6")
    
    # Verify isolation
    group1_session = temp_store.get_current(user_id, group1_id)
    group2_session = temp_store.get_current(user_id, group2_id)
    
    assert group1_session.model == "claude-opus-4-6"
    assert group2_session.model == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_process_message_sends_placeholder(temp_store, mock_feishu):
    """Test that process_message sends placeholder card"""
    user_id = "user_123"
    chat_id = "user_123"
    prompt = "Hello"
    
    with patch('main.run_claude', return_value=("Hi there!", "session_123")):
        await process_message(user_id, chat_id, prompt, temp_store, mock_feishu)
    
    # Verify placeholder was sent
    mock_feishu.send_card_to_user.assert_called_once()
    assert mock_feishu.send_card_to_user.call_args[1]['loading'] == True


@pytest.mark.asyncio
async def test_process_message_updates_with_full_response(temp_store, mock_feishu):
    """Test that process_message updates card with full response"""
    user_id = "user_123"
    chat_id = "user_123"
    prompt = "Hello"
    expected_response = "Hi there! How can I help you?"
    
    with patch('main.run_claude', return_value=(expected_response, "session_123")):
        await process_message(user_id, chat_id, prompt, temp_store, mock_feishu)
    
    # Verify card was updated with full response
    mock_feishu.update_card.assert_called_once()
    assert expected_response in str(mock_feishu.update_card.call_args)
```

- [ ] **Step 2: Run integration tests**

```bash
pytest tests/test_group_chat.py -v
```

Expected: All PASS

- [ ] **Step 3: Commit integration tests**

```bash
git add tests/test_group_chat.py
git commit -m "test: add integration tests for group chat

- Test session isolation between private and group chats
- Test multiple groups have independent sessions
- Test message processing with placeholder and update
- All tests passing

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 10: End-to-End Manual Testing

**Files:**
- None (manual testing)

- [ ] **Step 1: Run migration on real data**

```bash
# Backup first
cp ~/.feishu-claude/sessions.json ~/.feishu-claude/sessions.json.pre-migration

# Run migration
python migrate_sessions.py

# Verify
cat ~/.feishu-claude/sessions.json | jq 'keys'
```

- [ ] **Step 2: Start the bot**

```bash
python main.py
```

- [ ] **Step 3: Test private chat - short message**

Send in private chat:
```
Hello
```

Expected:
1. Placeholder appears
2. Full response appears after 3-5 seconds
3. No truncation

- [ ] **Step 4: Test private chat - long message**

Send in private chat:
```
Write a Python function to implement quicksort with detailed comments
```

Expected:
1. Placeholder appears
2. Full response with complete code appears after 10-20 seconds
3. No truncation

- [ ] **Step 5: Test private chat - /model command**

```
/model sonnet
```

Expected: Confirmation message

```
/status
```

Expected: Shows sonnet model

- [ ] **Step 6: Test group chat - add bot to group**

1. Create a test group in Feishu
2. Add the bot to the group

- [ ] **Step 7: Test group chat - send message**

Send in group:
```
Hello from group
```

Expected:
1. Bot responds (no @ needed)
2. Placeholder → full response

- [ ] **Step 8: Test group chat - /model command**

In group:
```
/model haiku
```

Expected: Model changed for this group

- [ ] **Step 9: Verify isolation - check /status in both chats**

Private chat:
```
/status
```

Expected: Shows sonnet

Group chat:
```
/status
```

Expected: Shows haiku

- [ ] **Step 10: Test /resume in group**

In group:
```
/resume
```

Expected: Shows only this group's session history, not private chat history

- [ ] **Step 11: Test multiple groups**

1. Create second test group
2. Add bot
3. Send message
4. Set different model
5. Verify isolation with /status

- [ ] **Step 12: Test error handling - timeout**

Send a message that would take >120 seconds (if possible), or temporarily reduce timeout in code.

Expected: Timeout error message after 120 seconds

- [ ] **Step 13: Document test results**

Create `docs/testing-results.md`:

```markdown
# Testing Results - 2026-03-15

## Migration
- ✅ Migration script runs successfully
- ✅ Backup created automatically
- ✅ Data structure validated
- ✅ Old sessions accessible after migration

## Private Chat
- ✅ Short messages: complete, no truncation
- ✅ Long messages: complete, no truncation
- ✅ /model command works
- ✅ /status shows correct info
- ✅ /resume shows private chat history only

## Group Chat
- ✅ Bot responds without @ mention
- ✅ Messages complete, no truncation
- ✅ /model command works per-group
- ✅ /status shows group-specific config
- ✅ /resume shows group-specific history

## Session Isolation
- ✅ Private and group sessions independent
- ✅ Multiple groups have independent sessions
- ✅ Model changes don't affect other chats
- ✅ /resume shows correct history per chat

## Error Handling
- ✅ Timeout after 120 seconds
- ✅ Placeholder remains if patch fails
- ✅ Retry logic works for send/update

## Performance
- Short messages: 3-5 seconds
- Long messages: 10-20 seconds
- No intermediate updates (as designed)
```

- [ ] **Step 14: Commit test results**

```bash
git add docs/testing-results.md
git commit -m "docs: add end-to-end testing results

All tests passing:
- Migration successful
- Private and group chat working
- Session isolation verified
- Error handling tested

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Summary

This plan implements:

1. **Session Store Refactoring** - Add `chat_id` parameter to all methods for session isolation
2. **Data Migration** - Migrate existing data to new format with backup and validation
3. **Group Chat Detection** - Extract chat info from message events
4. **One-Time Sending** - Remove streaming, accumulate full response before sending
5. **Commands Update** - Add `chat_id` support to all commands
6. **Feishu Client Simplification** - Keep only single-patch logic, add retry
7. **Testing** - Unit tests, integration tests, and end-to-end manual testing

**Key Principles:**
- TDD: Write tests first, then implementation
- DRY: Reuse chat_key normalization logic
- YAGNI: Remove unused streaming code
- Frequent commits: After each task completion

**Success Criteria:**
- ✅ All unit tests pass
- ✅ All integration tests pass
- ✅ Manual testing confirms no message truncation
- ✅ Private and group chats have isolated sessions
- ✅ Existing data migrated successfully

