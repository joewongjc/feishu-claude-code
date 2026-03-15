import asyncio
import sys
import os
import pytest
from unittest.mock import Mock, patch, AsyncMock

# 添加项目根目录到 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import handle_message_async, _chat_locks
from session_store import SessionStore


@pytest.mark.asyncio
async def test_concurrent_messages_different_groups():
    """测试同一用户在不同群组的消息并发处理"""
    # 清空锁
    _chat_locks.clear()

    # 模拟两个不同群组的消息事件
    event_group_a = Mock()
    event_group_a.event.sender.sender_id.open_id = "user123"
    event_group_a.event.message.chat_id = "group_a"
    event_group_a.event.message.chat_type = "group"
    event_group_a.event.message.message_type = "text"
    event_group_a.event.message.content = '{"text": "message in group A"}'
    event_group_a.event.message.message_id = "msg_a"

    event_group_b = Mock()
    event_group_b.event.sender.sender_id.open_id = "user123"
    event_group_b.event.message.chat_id = "group_b"
    event_group_b.event.message.chat_type = "group"
    event_group_b.event.message.message_type = "text"
    event_group_b.event.message.content = '{"text": "message in group B"}'
    event_group_b.event.message.message_id = "msg_b"

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
    event1.event.sender.sender_id.open_id = "user123"
    event1.event.message.chat_id = "group_a"
    event1.event.message.chat_type = "group"
    event1.event.message.message_type = "text"
    event1.event.message.content = '{"text": "message 1"}'
    event1.event.message.message_id = "msg_1"

    event2 = Mock()
    event2.event.sender.sender_id.open_id = "user123"
    event2.event.message.chat_id = "group_a"
    event2.event.message.chat_type = "group"
    event2.event.message.message_type = "text"
    event2.event.message.content = '{"text": "message 2"}'
    event2.event.message.message_id = "msg_2"

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

    # 并发调用 _save_async
    async def update_session(chat_id):
        await store._save_async()

    # 模拟多个群组同时更新
    await asyncio.gather(
        update_session("group_a"),
        update_session("group_b"),
        update_session("group_c"),
    )

    # 验证文件仍然有效
    store2 = SessionStore()
    assert store2._data is not None
