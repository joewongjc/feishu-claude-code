"""
通过 subprocess 调用本机 claude CLI，解析 stream-json 输出。
复用 ~/.claude/ 中已有的 Max 订阅登录凭证，无需额外 API Key。
"""

import asyncio
import json
import os
from typing import Optional

from bot_config import PERMISSION_MODE, CLAUDE_CLI


def _extract_text_content(value) -> str:
    """Extract final assistant text from Claude CLI result payload."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


def _apply_stream_payload(data: dict, stream_text: str, result_text: str, session_id: Optional[str]) -> tuple[str, str, Optional[str]]:
    """Apply one stream-json payload to the current parse state."""
    event_type = data.get("type")

    if event_type == "system":
        sid = data.get("session_id")
        if sid:
            session_id = sid

    elif event_type == "stream_event":
        evt = data.get("event", {})
        evt_type = evt.get("type")

        if evt_type == "content_block_delta":
            delta = evt.get("delta", {})
            if delta.get("type") == "text_delta":
                chunk = delta.get("text", "")
                if chunk:
                    stream_text += chunk

    elif event_type == "result":
        sid = data.get("session_id")
        if sid:
            session_id = sid
        final_text = _extract_text_content(data.get("result", ""))
        if final_text:
            result_text = final_text

    return stream_text, result_text, session_id


async def run_claude(
    message: str,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    permission_mode: Optional[str] = None,
) -> tuple[str, Optional[str], bool]:
    """
    调用 claude CLI 并返回完整回复（不再流式）。

    Returns:
        (full_response_text, new_session_id, used_fresh_session_fallback)
    """
    async def _run_once(active_session_id: Optional[str]) -> tuple[str, Optional[str], int, str]:
        cmd = [
            CLAUDE_CLI,
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode", permission_mode or PERMISSION_MODE,
        ]
        if active_session_id:
            cmd += ["--resume", active_session_id]
        if model:
            cmd += ["--model", model]

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or os.path.expanduser("~"),
            env=env,
            limit=10 * 1024 * 1024,  # 10MB，防止大响应超出默认 64KB 限制
        )

        proc.stdin.write((message + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()

        stream_text = ""
        result_text = ""
        new_session_id = None

        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            stream_text, result_text, new_session_id = _apply_stream_payload(
                data,
                stream_text,
                result_text,
                new_session_id,
            )

        stderr_output = await proc.stderr.read()
        await proc.wait()
        stderr_text = stderr_output.decode("utf-8", errors="replace").strip()
        final_text = (result_text or stream_text).strip()
        return final_text, new_session_id, proc.returncode, stderr_text

    final_text, new_session_id, returncode, stderr_text = await _run_once(session_id)
    used_fresh_session_fallback = False

    # Claude 的 session 与 cwd 不兼容时，CLI 有时直接 code=1 且 stderr 为空。
    # 这种场景自动退回新 session，避免用户必须手动 /new。
    if session_id and returncode != 0 and not stderr_text and not final_text:
        print("[run_claude] resume failed without stderr, retrying with fresh session", flush=True)
        final_text, new_session_id, returncode, stderr_text = await _run_once(None)
        used_fresh_session_fallback = True

    if returncode != 0:
        detail = stderr_text or "no stderr"
        if final_text:
            detail += f" (partial output length={len(final_text)})"
        # 如果有部分输出，返回给用户看而不是抛异常
        if final_text:
            return final_text, new_session_id, used_fresh_session_fallback
        raise RuntimeError(f"claude exited with code {returncode}: {detail}")

    return final_text, new_session_id, used_fresh_session_fallback
