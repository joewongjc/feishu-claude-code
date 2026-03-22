import asyncio
import json
import os
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

import main


def _make_event(text: str, open_id: str = "ou_test_user"):
    message = SimpleNamespace(
        message_type="text",
        chat_type="p2p",
        chat_id="oc_test_chat",
        content=json.dumps({"text": text}),
        message_id="om_test_message",
    )
    sender = SimpleNamespace(sender_id=SimpleNamespace(open_id=open_id))
    return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))


class HandleMessageQueueTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main._chat_locks.clear()

    async def test_queued_message_gets_immediate_acknowledgement(self):
        event = _make_event("second message")
        sender_open_id = event.event.sender.sender_id.open_id

        lock = asyncio.Lock()
        await lock.acquire()
        main._chat_locks[sender_open_id] = lock

        ack_mock = AsyncMock()
        process_mock = AsyncMock()

        with (
            patch.object(main.feishu, "send_text_to_user", ack_mock),
            patch.object(main, "_process_message", process_mock),
        ):
            task = asyncio.create_task(main.handle_message_async(event))
            await asyncio.sleep(0.05)

            self.assertEqual(ack_mock.await_count, 1)

            lock.release()
            await task
