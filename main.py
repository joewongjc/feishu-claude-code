"""
飞书 × Claude Code Bot
通过飞书 WebSocket 长连接接收私聊/群聊消息，调用本机 claude CLI 回复，支持流式卡片输出。

启动：python main.py
"""

import asyncio
import json
import sys
import os
import threading
import time
import traceback

# 确保项目目录在 sys.path 最前面
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lark_oapi as lark
from lark_oapi.api.im.v1.model import P2ImMessageReceiveV1

import bot_config as config
from feishu_client import FeishuClient
from session_store import SessionStore
from commands import parse_command, handle_command
from claude_runner import run_claude
from run_control import ActiveRun, ActiveRunRegistry, stop_run

# ── 看门狗：定时重启防止 WebSocket 假死 ──────────────────────

MAX_UPTIME = int(os.getenv("MAX_UPTIME_SECONDS", "0"))
MAX_IDLE_RESTART = int(os.getenv("MAX_IDLE_RESTART_SECONDS", "7200"))
_start_time = time.time()
_last_event = time.time()


def _watchdog():
    """后台线程，定期检查进程健康。异常时退出让 launchctl 拉起。"""
    while True:
        time.sleep(300)  # 每 5 分钟检查
        uptime = time.time() - _start_time
        idle = time.time() - _last_event

        if MAX_UPTIME > 0 and uptime > MAX_UPTIME:
            print(f"[watchdog] 运行 {uptime/3600:.1f}h，定时重启刷新连接", flush=True)
            os._exit(0)

        if MAX_IDLE_RESTART > 0 and idle > MAX_IDLE_RESTART:
            print(f"[watchdog] 空闲 {idle/60:.0f}min，主动重启刷新连接", flush=True)
            os._exit(0)

        print(f"[watchdog] uptime={uptime/3600:.1f}h idle={idle/60:.0f}min", flush=True)


# ── 全局单例 ──────────────────────────────────────────────────

lark_client = lark.Client.builder() \
    .app_id(config.FEISHU_APP_ID) \
    .app_secret(config.FEISHU_APP_SECRET) \
    .log_level(lark.LogLevel.INFO) \
    .build()

feishu = FeishuClient(lark_client, app_id=config.FEISHU_APP_ID, app_secret=config.FEISHU_APP_SECRET)
store = SessionStore()
_active_runs = ActiveRunRegistry()

# per-chat 消息队列锁，保证同一群组的消息串行处理，允许不同群组并发处理
_chat_locks: dict[str, asyncio.Lock] = {}
_MAX_CHAT_LOCKS = 200  # 防止无界增长


# ── /stop 命令处理 ───────────────────────────────────────────

async def _announce_stopped_run(active_run: ActiveRun):
    try:
        await feishu.update_card(active_run.card_msg_id, "⏹ 已停止当前任务")
    except Exception as exc:
        print(f"[warn] update stopped card failed: {exc}", flush=True)


async def _handle_stop_command(sender_open_id: str) -> str:
    active_run = _active_runs.get_run(sender_open_id)
    if active_run is None:
        return "当前没有正在运行的任务"
    if active_run.stop_requested:
        return "正在停止当前任务，请稍候"
    stopped = await stop_run(
        _active_runs,
        sender_open_id,
        on_stopped=_announce_stopped_run,
    )
    if not stopped:
        return "当前没有正在运行的任务"
    return "已发送停止请求"


# ── 核心消息处理（async）─────────────────────────────────────

def extract_chat_info(event: P2ImMessageReceiveV1) -> tuple[str, str, bool]:
    """
    Extract user_id, chat_id, and is_group from message event.

    Returns:
        (user_id, chat_id, is_group)
        - For private chat: chat_id = user_id
        - For group chat: chat_id = group's chat_id
    """
    sender = event.event.sender
    user_id = sender.sender_id.open_id

    message = event.event.message
    chat_type = message.chat_type
    chat_id_raw = message.chat_id

    is_group = (chat_type == "group")

    if is_group:
        chat_id = chat_id_raw
    else:
        chat_id = user_id

    return user_id, chat_id, is_group


async def handle_message_async(event: P2ImMessageReceiveV1):
    """异步处理一条飞书消息"""
    msg = event.event.message
    print(f"[收到消息] type={msg.message_type} chat={msg.chat_type}", flush=True)

    # Extract chat info (supports both private and group chats)
    user_id, chat_id, is_group = extract_chat_info(event)
    print(f"[Chat Info] user={user_id[:8]}... chat={chat_id[:8]}... is_group={is_group}", flush=True)

    # /stop 命令在锁外处理（不需要排队）
    if msg.message_type == "text":
        try:
            _text = json.loads(msg.content).get("text", "").strip()
        except Exception:
            _text = ""
        if _text.lower() in ("/stop", "@_user_1 /stop") or _text.strip().endswith("/stop"):
            reply = await _handle_stop_command(user_id)
            if is_group:
                await feishu.reply_card(msg.message_id, content=reply, loading=False)
            else:
                await feishu.send_card_to_user(user_id, content=reply, loading=False)
            return

    # 群聊只响应 @机器人 的消息
    if is_group:
        mentions = getattr(msg, 'mentions', None) or []
        if not mentions:
            return  # 没有 @mention，忽略

    # 获取该群组的队列锁，保证同一群组消息串行处理，不同群组可并发
    if chat_id not in _chat_locks:
        # 简单的 LRU 清理：超出上限时清掉所有锁（已释放的锁丢弃无害）
        if len(_chat_locks) >= _MAX_CHAT_LOCKS:
            _chat_locks.clear()
        _chat_locks[chat_id] = asyncio.Lock()
    lock = _chat_locks[chat_id]

    if lock.locked():
        await _notify_message_queued(user_id, is_group, msg)

    async with lock:
        try:
            await _process_message(user_id, chat_id, is_group, msg)
        except Exception as e:
            print(f"[error] 消息处理异常: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()


async def _notify_message_queued(user_id: str, is_group: bool, msg):
    """同一会话已有消息处理中时，立即给一条可见回执。"""
    try:
        notice = "前一条消息还在处理中，这条我已经收到了，结束后会按顺序继续。"
        if is_group:
            await feishu.reply_card(msg.message_id, content=notice, loading=False)
        else:
            await feishu.send_text_to_user(user_id, notice)
    except Exception as e:
        print(f"[warn] 发送排队提示失败: {type(e).__name__}: {e}", flush=True)


async def _process_message(user_id: str, chat_id: str, is_group: bool, msg):
    """实际处理消息的逻辑，在 per-chat lock 保护下执行"""
    print(f"[处理消息] user={user_id[:8]}... chat={chat_id[:8]}... is_group={is_group}", flush=True)
    text = ""
    img_path = None

    if msg.message_type == "text":
        try:
            text = json.loads(msg.content).get("text", "").strip()
        except Exception:
            return
        if not text:
            return

        # 群聊：去掉 @mention 占位符
        if is_group:
            mentions = getattr(msg, 'mentions', None) or []
            for mention in mentions:
                key = getattr(mention, 'key', '')
                if key:
                    text = text.replace(key, '').strip()
            if not text:
                return

        print(f"[文本] {text[:50]}", flush=True)

    elif msg.message_type == "image":
        try:
            image_key = json.loads(msg.content).get("image_key", "")
            if not image_key:
                return
            img_path = await feishu.download_image(msg.message_id, image_key)
            text = f"[用户发送了一张图片，路径：{img_path}，请读取并分析这张图片，直接回复用中文]"
        except Exception as e:
            print(f"[error] 下载图片失败: {e}")
            if is_group:
                try:
                    await feishu.reply_card(msg.message_id, content=f"❌ 下载图片失败：{e}", loading=False)
                except Exception:
                    pass
            else:
                await feishu.send_text_to_user(user_id, f"❌ 下载图片失败：{e}")
            return

    else:
        return  # 不支持的消息类型

    # ── 斜杠命令 ──────────────────────────────────────────────
    parsed = parse_command(text)
    if parsed:
        cmd, args = parsed
        reply = await handle_command(cmd, args, user_id, chat_id, store)
        if reply is not None:
            if cmd == "resume" and not args:
                # /resume 命令特殊处理：发送文本消息
                if is_group:
                    await feishu.reply_card(msg.message_id, content=reply, loading=False)
                else:
                    await feishu.send_text_to_user(user_id, reply)
            else:
                if is_group:
                    await feishu.reply_card(msg.message_id, content=reply, loading=False)
                else:
                    await feishu.send_card_to_user(user_id, content=reply, loading=False)
            return
        # reply is None → 不是 bot 命令，当作普通消息（含 /xxx）转发给 Claude

    # ── 普通消息 → 调用 Claude ──────────────────────────────
    session = await store.get_current(user_id, chat_id)
    print(f"[Claude] session={session.session_id} model={session.model}", flush=True)

    # 1. 发送"思考中"占位卡片，拿到 message_id
    try:
        if is_group:
            card_msg_id = await feishu.reply_card(msg.message_id, loading=True)
        else:
            card_msg_id = await feishu.send_card_to_user(user_id, loading=True)
        print(f"[卡片] card_msg_id={card_msg_id}", flush=True)
    except Exception as e:
        print(f"[error] 发送占位卡片失败: {e}", flush=True)
        if is_group:
            try:
                await feishu.reply_card(msg.message_id, content=f"❌ 发送消息失败：{e}", loading=False)
            except Exception:
                pass
        else:
            await feishu.send_text_to_user(user_id, f"❌ 发送消息失败：{e}")
        return

    active_run = _active_runs.start_run(user_id, card_msg_id)

    # 2. 流式回调
    accumulated = ""
    chars_since_push = 0

    async def push(content: str):
        try:
            await feishu.update_card(card_msg_id, content)
        except Exception as push_err:
            print(f"[warn] push 失败: {push_err}", flush=True)

    async def on_tool_use(name: str, inp: dict):
        nonlocal accumulated, chars_since_push
        # AskUserQuestion: 把问题内容直接作为正文显示
        if name.lower() == "askuserquestion":
            question = inp.get("question", inp.get("text", ""))
            if question:
                accumulated += f"\n\n❓ **等待回复：**\n{question}"
                chars_since_push = 0
                await push(accumulated)
                return
        tool_line = _format_tool(name, inp)
        display = f"{tool_line}\n\n{accumulated}" if accumulated else tool_line
        await push(display)

    async def on_text_chunk(chunk: str):
        nonlocal accumulated, chars_since_push
        accumulated += chunk
        chars_since_push += len(chunk)
        if chars_since_push >= config.STREAM_CHUNK_SIZE:
            await push(accumulated)
            chars_since_push = 0

    # 3. 运行 Claude
    claude_msg = text
    if not session.session_id:
        claude_msg = (
            "[环境：用户通过飞书发送消息，无交互式UI。"
            "当需要用户做选择时，用编号列表呈现选项（1. 2. 3.），"
            "最后加一个「其他（请说明）」选项，用户回复数字即可。"
            "简单确认用 Y/N。]\n\n" + text
        )
    try:
        print(f"[run_claude] 开始调用...", flush=True)
        full_text, new_session_id, used_fresh_session_fallback = await run_claude(
            message=claude_msg,
            session_id=session.session_id,
            model=session.model,
            cwd=session.cwd,
            permission_mode=session.permission_mode,
            on_text_chunk=on_text_chunk,
            on_tool_use=on_tool_use,
            on_process_start=lambda proc: _active_runs.attach_process(user_id, proc),
        )
        print(f"[run_claude] 完成, session={new_session_id}", flush=True)
    except Exception as e:
        if active_run.stop_requested:
            return
        print(f"[error] Claude 运行失败: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        try:
            await feishu.update_card(card_msg_id, f"❌ Claude 执行出错：{type(e).__name__}: {e}")
        except Exception:
            pass
        return

    # 4. 最终更新卡片为完整内容
    final = full_text or "（无输出）"
    if used_fresh_session_fallback:
        final = (
            "⚠️ 检测到工作目录已变化，旧会话无法继续。"
            "本次已自动切换到新 session。\n\n" + final
        )
    try:
        await feishu.update_card(card_msg_id, final)
    except Exception as e:
        print(f"[error] 更新卡片失败: {e}", flush=True)

    # 5. 更新 session 状态
    if new_session_id:
        await store.on_claude_response(user_id, chat_id, new_session_id, text)


def _format_tool(name: str, inp: dict) -> str:
    """格式化工具调用的进度提示"""
    n = name.lower()
    if n == "bash":
        cmd = inp.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"🔧 **执行命令：** `{cmd}`" if cmd else f"🔧 **执行命令...**"
    elif n in ("read_file", "read"):
        return f"📄 **读取：** `{inp.get('file_path', inp.get('path', ''))}`"
    elif n in ("write_file", "write"):
        return f"✏️ **写入：** `{inp.get('file_path', inp.get('path', ''))}`"
    elif n in ("edit_file", "edit"):
        return f"✂️ **编辑：** `{inp.get('file_path', inp.get('path', ''))}`"
    elif n in ("glob",):
        return f"🔍 **搜索文件：** `{inp.get('pattern', '')}`"
    elif n in ("grep",):
        return f"🔎 **搜索内容：** `{inp.get('pattern', '')}`"
    elif n == "task":
        return f"🤖 **子任务：** {inp.get('description', inp.get('prompt', '')[:40])}"
    elif n == "webfetch":
        return f"🌐 **抓取网页...**"
    elif n == "websearch":
        return f"🔍 **搜索：** {inp.get('query', '')}"
    else:
        return f"⚙️ **{name}**"


# ── 飞书事件回调（同步）→ 调度异步任务 ───────────────────────

def on_message_receive(data: P2ImMessageReceiveV1) -> None:
    """
    飞书 SDK 同步回调。
    ws.Client 内部运行 asyncio loop，此处显式创建后台任务并记录异常。
    """
    global _last_event
    _last_event = time.time()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    task = loop.create_task(handle_message_async(data))
    task.add_done_callback(_log_background_task_result)


def _log_background_task_result(task: asyncio.Task):
    """把后台任务异常打到主日志，避免静默丢失。"""
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        print("[warn] 后台消息任务被取消", flush=True)
        return
    except Exception as e:
        print(f"[error] 读取后台任务结果失败: {type(e).__name__}: {e}", flush=True)
        return

    if exc is not None:
        print(f"[error] 后台消息任务异常: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stdout)
        sys.stdout.flush()


# ── 启动 ──────────────────────────────────────────────────────

def main():
    print("🚀 飞书 Claude Bot 启动中...")
    print(f"   App ID      : {config.FEISHU_APP_ID}")
    print(f"   默认模型    : {config.DEFAULT_MODEL}")
    print(f"   默认工作目录: {config.DEFAULT_CWD}")
    print(f"   权限模式    : {config.PERMISSION_MODE}")

    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message_receive) \
        .build()

    ws_client = lark.ws.Client(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )

    # 启动看门狗线程
    t = threading.Thread(target=_watchdog, daemon=True)
    t.start()

    print("✅ 连接飞书 WebSocket 长连接（自动重连）...")
    ws_client.start()  # 阻塞，内部运行 asyncio loop


if __name__ == "__main__":
    main()
