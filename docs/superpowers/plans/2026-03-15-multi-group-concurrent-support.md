# 多群组并发支持 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 改为 per-chat 锁 + SessionStore 加锁，支持同一用户在多个群组同时调用 Claude，而不是串行处理。

**Architecture:** 将锁粒度从 per-user 改为 per-chat，允许不同群组并行处理消息。同时在 SessionStore 中添加全局锁保护文件写入，防止并发修改导致的数据丢失。

**Tech Stack:** Python asyncio, JSON file storage

---

## Chunk 1: 修改 main.py 的锁机制

### Task 1: 修改 _user_locks 为 _chat_locks

**Files:**
- Modify: `main.py:61-110`

- [ ] **Step 1: 读取 main.py 的锁相关代码**

Run: `head -n 120 main.py | tail -n 60`

Expected: 看到 `_user_locks` 定义和使用

- [ ] **Step 2: 替换 _user_locks 为 _chat_locks**

在 `main.py:61` 将：
```python
_user_locks: dict[str, asyncio.Lock] = {}
```

改为：
```python
_chat_locks: dict[str, asyncio.Lock] = {}
```

- [ ] **Step 3: 更新 handle_message_async 中的锁获取逻辑**

在 `main.py:104-106` 将：
```python
if user_id not in _user_locks:
    _user_locks[user_id] = asyncio.Lock()
lock = _user_locks[user_id]
```

改为：
```python
if chat_id not in _chat_locks:
    _chat_locks[chat_id] = asyncio.Lock()
lock = _chat_locks[chat_id]
```

- [ ] **Step 4: 验证改动**

Run: `grep -n "_chat_locks\|_user_locks" main.py`

Expected: 只看到 `_chat_locks`，没有 `_user_locks`

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "refactor: change from per-user lock to per-chat lock for concurrent group support"
```

---

## Chunk 2: 修改 SessionStore 添加全局锁

### Task 2: 在 SessionStore 中添加 _save_lock

**Files:**
- Modify: `session_store.py:286-304`

- [ ] **Step 1: 在 SessionStore.__init__ 中添加 _save_lock**

在 `session_store.py:287-290` 的 `__init__` 方法中，在 `self._data: dict = self._load()` 之前添加：

```python
def __init__(self):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    self._save_lock = asyncio.Lock()  # 保护 _save() 的全局锁
    self._data: dict = self._load()
    self._dedup_all_histories()
```

- [ ] **Step 2: 验证改动**

Run: `grep -n "_save_lock" session_store.py`

Expected: 看到 `_save_lock` 在 `__init__` 中被初始化

- [ ] **Step 3: Commit**

```bash
git add session_store.py
git commit -m "feat: add _save_lock to SessionStore for concurrent write protection"
```

---

## Chunk 3: 创建异步 _save_async 方法

### Task 3: 添加 _save_async 方法

**Files:**
- Modify: `session_store.py:301-304`

- [ ] **Step 1: 在 _save 方法后添加 _save_async 方法**

在 `session_store.py:301-303` 的 `_save()` 方法后添加：

```python
async def _save_async(self):
    """异步保存，使用锁保护并发写入"""
    async with self._save_lock:
        with open(SESSIONS_FILE, "w") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
```

- [ ] **Step 2: 验证改动**

Run: `grep -n "def _save" session_store.py`

Expected: 看到 `_save` 和 `_save_async` 两个方法

- [ ] **Step 3: Commit**

```bash
git add session_store.py
git commit -m "feat: add _save_async method with lock protection"
```

---

## Chunk 4: 更新所有 _save() 调用为 _save_async()

### Task 4: 更新 SessionStore 内部的 _save 调用

**Files:**
- Modify: `session_store.py` (多处)

- [ ] **Step 1: 找出所有 _save() 调用**

Run: `grep -n "self._save()" session_store.py`

Expected: 看到约 5-6 处调用

- [ ] **Step 2: 更新 _dedup_all_histories 中的 _save 调用**

在 `session_store.py:326` 将：
```python
self._save()
```

改为：
```python
asyncio.run(self._save_async())
```

- [ ] **Step 3: 更新 _ensure_chat_data 中的 _save 调用**

在 `session_store.py:381` 将：
```python
self._save()
```

改为：
```python
asyncio.run(self._save_async())
```

- [ ] **Step 4: 更新 batch_set_summaries 中的 _save 调用**

在 `session_store.py:393` 将：
```python
self._save()
```

改为：
```python
asyncio.run(self._save_async())
```

- [ ] **Step 5: 更新 on_claude_response 中的 _save 调用**

在 `session_store.py:436` 将：
```python
self._save()
```

改为：
```python
asyncio.run(self._save_async())
```

- [ ] **Step 6: 更新 new_session 中的 _save 调用**

在 `session_store.py:477` 将：
```python
self._save()
```

改为：
```python
asyncio.run(self._save_async())
```

- [ ] **Step 7: 更新其他 _save 调用**

Run: `grep -n "self._save()" session_store.py`

Expected: 没有结果（所有调用都已更新）

- [ ] **Step 8: Commit**

```bash
git add session_store.py
git commit -m "refactor: replace all _save() calls with _save_async() for concurrent safety"
```

---

## Chunk 5: 测试多群组并发场景

### Task 5: 编写测试验证多群组并发

**Files:**
- Create: `tests/test_concurrent_groups.py`

- [ ] **Step 1: 创建测试文件**

```python
import asyncio
import pytest
from unittest.mock import Mock, patch, AsyncMock
from main import handle_message_async, _chat_locks
from session_store import SessionStore


@pytest.mark.asyncio
async def test_concurrent_messages_different_groups():
    """测试同一用户在不同群组的消息并发处理"""
    # 清空锁
    _chat_locks.clear()

    # 模拟两个不同群组的消息事件
    event_group_a = Mock()
    event_group_a.sender.id = "user123"
    event_group_a.chat.id = "group_a"
    event_group_a.chat.chat_type = "group"
    event_group_a.message.content = "message in group A"

    event_group_b = Mock()
    event_group_b.sender.id = "user123"
    event_group_b.chat.id = "group_b"
    event_group_b.chat.chat_type = "group"
    event_group_b.message.content = "message in group B"

    # 验证两个群组使用不同的锁
    with patch('main._process_message', new_callable=AsyncMock) as mock_process:
        # 并发处理两个消息
        await asyncio.gather(
            handle_message_async(event_group_a),
            handle_message_async(event_group_b),
        )

        # 验证两个消息都被处理
        assert mock_process.call_count == 2

        # 验证使用了不同的锁
        assert "group_a" in _chat_locks
        assert "group_b" in _chat_locks
        assert _chat_locks["group_a"] is not _chat_locks["group_b"]


@pytest.mark.asyncio
async def test_same_group_messages_serialized():
    """测试同一群组的消息仍然串行处理"""
    _chat_locks.clear()

    event1 = Mock()
    event1.sender.id = "user123"
    event1.chat.id = "group_a"
    event1.chat.chat_type = "group"
    event1.message.content = "message 1"

    event2 = Mock()
    event2.sender.id = "user123"
    event2.chat.id = "group_a"
    event2.chat.chat_type = "group"
    event2.message.content = "message 2"

    with patch('main._process_message', new_callable=AsyncMock) as mock_process:
        # 并发发送两个消息到同一群组
        await asyncio.gather(
            handle_message_async(event1),
            handle_message_async(event2),
        )

        # 验证两个消息都被处理
        assert mock_process.call_count == 2

        # 验证使用了同一个锁
        assert _chat_locks["group_a"].locked() == False  # 锁已释放


@pytest.mark.asyncio
async def test_session_store_concurrent_writes():
    """测试 SessionStore 并发写入的安全性"""
    store = SessionStore()

    # 并发调用 on_claude_response
    async def update_session(chat_id, session_id):
        await store._save_async()

    # 模拟多个群组同时更新
    await asyncio.gather(
        update_session("group_a", "session_1"),
        update_session("group_b", "session_2"),
        update_session("group_c", "session_3"),
    )

    # 验证文件仍然有效
    store2 = SessionStore()
    assert store2._data is not None
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_concurrent_groups.py -v`

Expected: 测试通过（因为我们已经实现了功能）

- [ ] **Step 3: Commit**

```bash
git add tests/test_concurrent_groups.py
git commit -m "test: add concurrent group message handling tests"
```

---

## Chunk 6: 集成测试和验证

### Task 6: 运行完整测试套件

**Files:**
- Test: `tests/`

- [ ] **Step 1: 运行所有现有测试**

Run: `pytest tests/ -v`

Expected: 所有测试通过

- [ ] **Step 2: 运行新的并发测试**

Run: `pytest tests/test_concurrent_groups.py -v`

Expected: 所有并发测试通过

- [ ] **Step 3: 验证 main.py 的语法**

Run: `python -m py_compile main.py session_store.py`

Expected: 无错误

- [ ] **Step 4: 检查是否有遗漏的 _save() 调用**

Run: `grep -r "self._save()" . --include="*.py" | grep -v ".venv" | grep -v "__pycache__"`

Expected: 没有结果

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test: verify all tests pass with concurrent changes"
```

---

## Chunk 7: 文档和总结

### Task 7: 更新 README 文档

**Files:**
- Modify: `README.md` (如果存在)

- [ ] **Step 1: 检查是否有 README**

Run: `ls -la README.md 2>/dev/null || echo "No README found"`

- [ ] **Step 2: 如果存在，添加并发支持说明**

在 README 的适当位置添加：

```markdown
## 并发支持

本项目支持同一用户在多个群组同时调用 Claude：

- 使用 per-chat 锁机制，允许不同群组的消息并行处理
- SessionStore 使用全局锁保护文件写入，确保数据一致性
- 同一群组内的消息仍然串行处理，保证顺序
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add concurrent group support documentation"
```

---

## 验收标准

✅ 所有测试通过
✅ 多群组消息可并行处理
✅ 单群组消息保持串行
✅ SessionStore 数据一致性有保证
✅ 代码无语法错误
✅ 所有改动已提交

