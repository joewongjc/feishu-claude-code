"""
通过 subprocess 调用本机 claude CLI，解析 stream-json 输出。
复用 ~/.claude/ 中已有的 Max 订阅登录凭证，无需额外 API Key。
"""

import asyncio
import json
import os
import subprocess as sp
from typing import Callable, Optional

from bot_config import PERMISSION_MODE, CLAUDE_CLI

IDLE_TIMEOUT = 300  # 5 分钟无输出且无子进程，视为挂死
_CHECK_INTERVAL = 30  # 静默时每 30 秒检查一次子进程


def _has_children(pid: int) -> bool:
    """进程是否有活跃子进程（说明在跑 bash 命令、编译等）。"""
    try:
        result = sp.run(["pgrep", "-P", str(pid)], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


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


async def _fire_callback(cb, *args):
    if cb is None:
        return
    if asyncio.iscoroutinefunction(cb):
        await cb(*args)
    else:
        cb(*args)


async def run_claude(
    message: str,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    permission_mode: Optional[str] = None,
    on_text_chunk: Optional[Callable[[str], None]] = None,
    on_tool_use: Optional[Callable[[str, dict], None]] = None,
    on_process_start: Optional[Callable[[asyncio.subprocess.Process], None]] = None,
) -> tuple[str, Optional[str], bool]:
    """
    调用 claude CLI 并流式解析输出。

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
            limit=10 * 1024 * 1024,
        )

        await _fire_callback(on_process_start, proc)

        proc.stdin.write((message + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()

        full_text = ""
        new_session_id = None
        pending_tool_name = ""
        pending_tool_input_json = ""

        idle_seconds = 0

        try:
            while True:
                try:
                    raw_line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=_CHECK_INTERVAL
                    )
                    idle_seconds = 0  # 收到输出，重置计时
                except asyncio.TimeoutError:
                    if _has_children(proc.pid):
                        # 有子进程在跑（编译/下载等），继续等
                        idle_seconds = 0
                        continue
                    idle_seconds += _CHECK_INTERVAL
                    if idle_seconds >= IDLE_TIMEOUT:
                        proc.kill()
                        await proc.wait()
                        raise RuntimeError(
                            f"Claude 执行超时（{IDLE_TIMEOUT}秒无输出且无活跃子进程），已终止进程"
                        )
                    continue

                if not raw_line:  # EOF
                    break

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = data.get("type")

                if event_type == "system":
                    sid = data.get("session_id")
                    if sid:
                        new_session_id = sid

                elif event_type == "stream_event":
                    evt = data.get("event", {})
                    evt_type = evt.get("type")

                    if evt_type == "content_block_delta":
                        delta = evt.get("delta", {})
                        delta_type = delta.get("type")

                        if delta_type == "text_delta":
                            chunk = delta.get("text", "")
                            if chunk:
                                full_text += chunk
                                await _fire_callback(on_text_chunk, chunk)

                        elif delta_type == "input_json_delta":
                            pending_tool_input_json += delta.get("partial_json", "")

                    elif evt_type == "content_block_start":
                        block = evt.get("content_block", {})
                        if block.get("type") == "tool_use":
                            pending_tool_name = block.get("name", "")
                            pending_tool_input_json = ""
                            await _fire_callback(on_tool_use, pending_tool_name, {})

                    elif evt_type == "content_block_stop":
                        if pending_tool_name:
                            inp = {}
                            if pending_tool_input_json:
                                try:
                                    inp = json.loads(pending_tool_input_json)
                                except json.JSONDecodeError:
                                    pass
                            await _fire_callback(on_tool_use, pending_tool_name, inp)
                        pending_tool_name = ""
                        pending_tool_input_json = ""

                elif event_type == "result":
                    sid = data.get("session_id")
                    if sid:
                        new_session_id = sid
                    final_text = _extract_text_content(data.get("result", ""))
                    if final_text:
                        full_text = final_text

        except RuntimeError:
            raise

        stderr_output = await proc.stderr.read()
        await proc.wait()
        stderr_text = stderr_output.decode("utf-8", errors="replace").strip()
        return full_text.strip(), new_session_id, proc.returncode, stderr_text

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
