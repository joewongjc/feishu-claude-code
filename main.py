"""
飞书 × Claude Code Bot
通过飞书 WebSocket 长连接接收私聊消息，调用本机 claude CLI 回复，支持流式卡片输出。

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

# ── 看门狗：定时重启防止 WebSocket 假死 ──────────────────────

MAX_UPTIME = 4 * 3600   # 最长运行 4 小时后主动重启
_start_time = time.time()
_last_event = time.time()


def _watchdog():
    """后台线程，定期检查进程健康。异常时退出让 launchctl 拉起。"""
    while True:
        time.sleep(300)  # 每 5 分钟检查
        uptime = time.time() - _start_time
        idle = time.time() - _last_event

        if uptime > MAX_UPTIME:
            print(f"[watchdog] 运行 {uptime/3600:.1f}h，定时重启刷新连接", flush=True)
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

# per-chat 消息队列锁，保证同一群组的消息串行处理，允许不同群组并发处理
_chat_locks: dict[str, asyncio.Lock] = {}


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

    # Check message type to determine if it's a group chat
    message = event.event.message
    chat_type = message.chat_type
    chat_id_raw = message.chat_id

    # chat_type: "p2p" for private, "group" for group
    is_group = (chat_type == "group")

    if is_group:
        chat_id = chat_id_raw
    else:
        # Private chat: use user_id as chat_id
        chat_id = user_id

    return user_id, chat_id, is_group

async def handle_message_async(event: P2ImMessageReceiveV1):
    """异步处理一条飞书消息"""
    msg = event.event.message
    print(f"[收到消息] type={msg.message_type} chat={msg.chat_type}", flush=True)

    # Extract chat info (supports both private and group chats)
    user_id, chat_id, is_group = extract_chat_info(event)
    print(f"[Chat Info] user={user_id[:8]}... chat={chat_id[:8]}... is_group={is_group}", flush=True)

    # 获取该群组的队列锁，保证同一群组消息串行处理，不同群组可并发
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    lock = _chat_locks[chat_id]

    async with lock:
        try:
            await _process_message(user_id, chat_id, is_group, msg)
        except Exception as e:
            print(f"[error] 消息处理异常: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()


async def _process_message(user_id: str, chat_id: str, is_group: bool, msg):
    """实际处理消息的逻辑，在 per-user lock 保护下执行"""
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
                    # 群组中回复
                    await feishu.send_text_to_user(user_id, reply)  # 暂时私聊通知
                else:
                    await feishu.send_text_to_user(user_id, reply)
            else:
                # 其他命令：发送卡片消息
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
            # 群组中回复原消息
            card_msg_id = await feishu.reply_card(msg.message_id, loading=True)
        else:
            # 私聊发送新消息
            card_msg_id = await feishu.send_card_to_user(user_id, loading=True)
        print(f"[卡片] card_msg_id={card_msg_id}", flush=True)
    except Exception as e:
        print(f"[error] 发送占位卡片失败: {e}", flush=True)
        if is_group:
            # 群组中回复错误
            try:
                await feishu.reply_card(msg.message_id, content=f"❌ 发送消息失败：{e}", loading=False)
            except Exception:
                pass
        else:
            await feishu.send_text_to_user(user_id, f"❌ 发送消息失败：{e}")
        return

    # 2. 运行 Claude，等待完整回复
    # 新 session 第一条消息带环境提示
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
        )
        print(f"[run_claude] 完成, session={new_session_id}", flush=True)
    except Exception as e:
        print(f"[error] Claude 运行失败: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        try:
            await feishu.update_card(card_msg_id, f"❌ Claude 执行出错：{type(e).__name__}: {e}")
        except Exception:
            pass
        return

    # 3. 一次性更新卡片为完整内容
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
        # 占位卡片保留，用户知道在处理中

    # 4. 更新 session 状态
    if new_session_id:
        # Note: on_claude_response still uses old signature, need to update
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
    ws.Client 内部运行 asyncio loop，此处用 ensure_future 调度异步任务。
    """
    global _last_event
    _last_event = time.time()
    asyncio.ensure_future(handle_message_async(data))


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
