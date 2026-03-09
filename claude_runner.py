"""
通过 subprocess 调用本机 claude CLI，解析 stream-json 输出。
复用 ~/.claude/ 中已有的 Max 订阅登录凭证，无需额外 API Key。
"""

import asyncio
import json
import os
from typing import Callable, Optional

from bot_config import PERMISSION_MODE, CLAUDE_CLI


async def run_claude(
    message: str,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    permission_mode: Optional[str] = None,
    on_text_chunk: Optional[Callable[[str], None]] = None,
    on_tool_use: Optional[Callable[[str, dict], None]] = None,
) -> tuple[str, Optional[str]]:
    """
    调用 claude CLI 并流式解析输出。

    Returns:
        (full_response_text, new_session_id)
    """
    cmd = [
        CLAUDE_CLI,
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode", permission_mode or PERMISSION_MODE,
    ]
    if session_id:
        cmd += ["--resume", session_id]
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

    full_text = ""
    new_session_id = None

    # 跟踪当前正在构建的 tool_use block
    pending_tool_name = ""
    pending_tool_input_json = ""

    async def _fire_tool_use(name: str, inp: dict):
        if on_tool_use:
            if asyncio.iscoroutinefunction(on_tool_use):
                await on_tool_use(name, inp)
            else:
                on_tool_use(name, inp)

    # 空闲超时：每次收到数据重置计时，长任务不会被误杀
    IDLE_TIMEOUT = 300  # 5 分钟无任何输出视为挂死

    try:
        while True:
            try:
                raw_line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=IDLE_TIMEOUT
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(
                    f"Claude 执行超时（{IDLE_TIMEOUT}秒无输出），已终止进程"
                )

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
                            if on_text_chunk:
                                if asyncio.iscoroutinefunction(on_text_chunk):
                                    await on_text_chunk(chunk)
                                else:
                                    on_text_chunk(chunk)

                    elif delta_type == "input_json_delta":
                        # 积累 tool_use 的 input JSON 片段
                        pending_tool_input_json += delta.get("partial_json", "")

                elif evt_type == "content_block_start":
                    block = evt.get("content_block", {})
                    if block.get("type") == "tool_use":
                        pending_tool_name = block.get("name", "")
                        pending_tool_input_json = ""
                        # 立即触发一次回调（name 已知，input 还空），用于显示进度
                        await _fire_tool_use(pending_tool_name, {})

                elif evt_type == "content_block_stop":
                    # tool_use block 结束，input 已完整，再触发一次带完整参数的回调
                    if pending_tool_name and pending_tool_input_json:
                        try:
                            inp = json.loads(pending_tool_input_json)
                        except json.JSONDecodeError:
                            inp = {}
                        await _fire_tool_use(pending_tool_name, inp)
                    pending_tool_name = ""
                    pending_tool_input_json = ""

            elif event_type == "result":
                sid = data.get("session_id")
                if sid:
                    new_session_id = sid
                if not full_text:
                    full_text = data.get("result", "")
    except RuntimeError:
        raise  # 重新抛出超时异常

    stderr_output = await proc.stderr.read()
    await proc.wait()

    if proc.returncode != 0 and not full_text:
        stderr_text = stderr_output.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"claude exited with code {proc.returncode}: {stderr_text}")

    return full_text.strip(), new_session_id
