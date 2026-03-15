import asyncio
import os
import sys

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_runner import run_claude


class FakeStdin:
    def __init__(self):
        self.buffer = b""
        self.closed = False

    def write(self, data: bytes):
        self.buffer += data

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class FakeStdout:
    def __init__(self, lines: list[bytes]):
        self._lines = iter(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._lines)
        except StopIteration:
            raise StopAsyncIteration


class FakeStderr:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self):
        return self._data


class FakeProc:
    def __init__(self, stdout_lines: list[bytes], stderr: bytes = b"", returncode: int = 0):
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(stdout_lines)
        self.stderr = FakeStderr(stderr)
        self.returncode = returncode

    async def wait(self):
        return self.returncode


def test_run_claude_prefers_final_result_over_partial_deltas(monkeypatch):
    proc = FakeProc([
        b'{"type":"system","session_id":"sid_123"}\n',
        b'{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}}\n',
        b'{"type":"result","session_id":"sid_123","result":"Hello world"}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    text, session_id, used_fallback = asyncio.run(run_claude("hi"))

    assert text == "Hello world"
    assert session_id == "sid_123"
    assert used_fallback is False
    assert proc.stdin.buffer.endswith(b"hi\n")
    assert proc.stdin.closed is True


def test_run_claude_raises_on_nonzero_exit_even_with_partial_output(monkeypatch):
    proc = FakeProc([
        b'{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"partial"}}}\n',
    ], stderr=b"boom", returncode=1)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match=r"partial output length=7"):
        asyncio.run(run_claude("hi"))


def test_run_claude_retries_without_resume_on_empty_stderr_failure(monkeypatch):
    first = FakeProc([], stderr=b"", returncode=1)
    second = FakeProc([
        b'{"type":"system","session_id":"sid_new"}\n',
        b'{"type":"result","session_id":"sid_new","result":"fresh answer"}\n',
    ])
    procs = iter([first, second])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return next(procs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    text, session_id, used_fallback = asyncio.run(run_claude("hi", session_id="sid_old"))

    assert text == "fresh answer"
    assert session_id == "sid_new"
    assert used_fallback is True
    assert first.stdin.closed is True
    assert second.stdin.closed is True
