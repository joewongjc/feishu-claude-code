"""
斜杠命令解析与处理。
返回要发送给用户的回复文本。
"""

import getpass
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from typing import Optional, Tuple

from bot_config import CLAUDE_CLI, DEFAULT_CWD
from session_store import SessionStore, scan_cli_sessions, generate_summary, _get_api_token, _write_custom_title

PLUGINS_DIR = os.path.expanduser("~/.claude/plugins")


VALID_MODES = {
    "default": "每次工具调用需确认",
    "acceptEdits": "自动接受文件编辑，其余需确认",
    "plan": "只规划不执行工具",
    "bypassPermissions": "全部自动执行（无确认）",
    "dontAsk": "全部自动执行（静默）",
}

MODE_ALIASES = {
    "bypass": "bypassPermissions",
    "accept": "acceptEdits",
    "auto": "bypassPermissions",
}

MODEL_ALIASES = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

HELP_TEXT = """\
📖 **可用命令**

**Bot 管理：**
`/help` — 显示此帮助
`/new` 或 `/clear` — 开始新 session
`/resume` — 查看历史 sessions / `/resume [序号]` 恢复
`/model [名称]` — 切换模型（opus / sonnet / haiku 或完整 ID）
`/mode [模式]` — 切换权限模式（default / plan / acceptEdits / bypassPermissions）
`/status` — 显示当前 session 信息
`/cd [路径]` — 切换工具执行的工作目录
`/ls [路径]` — 查看当前工作目录下的文件/目录
`/workspace` 或 `/ws` — 保存/切换群组工作空间

**查看能力：**
`/skills` — 列出已安装的 Claude Skills
`/mcp` — 列出已配置的 MCP Servers
`/usage` — 查看 Claude Max 订阅用量百分比和重置时间


**Claude Skills（直接转发给 Claude 执行）：**
`/commit` — 提交代码
其他 `/xxx` — 自动转发给 Claude 处理

**MCP 工具：** 已配置的 MCP servers 自动可用，直接对话即可调用。

**发送任意普通消息即可与 Claude 对话。**\
"""


def parse_command(text: str) -> Optional[Tuple[str, str]]:
    """
    尝试解析斜杠命令。
    返回 (command, args) 或 None（不是命令）。
    """
    text = text.strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split(None, 1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return cmd, args


# Bot 自身处理的命令，其余 /xxx 转发给 Claude
BOT_COMMANDS = {
    "help", "h", "new", "clear", "resume", "model", "mode", "status", "cd", "ls",
    "workspace", "ws", "skills", "mcp", "usage",
}


async def _build_session_list(user_id: str, chat_id: str, store: SessionStore) -> list[dict]:
    """构建合并、去重、排序后的 session 列表（不含当前 session）。
    /resume 列表展示和 /resume N 选择都用这一个函数，保证索引一致。"""
    cur_sid = (await store.get_current_raw(user_id, chat_id)).get("session_id")

    cli_all = scan_cli_sessions(30)
    cli_preview_map = {s["session_id"]: s for s in cli_all}

    feishu_sessions = [
        {**s, "source": "feishu"} for s in await store.list_sessions(user_id, chat_id)
    ]
    for s in feishu_sessions:
        cli_info = cli_preview_map.get(s["session_id"])
        if cli_info and cli_info.get("preview"):
            s["preview"] = cli_info["preview"]

    feishu_ids = {s["session_id"] for s in feishu_sessions}
    cli_sessions = [
        s for s in cli_all
        if s["session_id"] not in feishu_ids and len(s.get("preview", "")) > 5
    ]
    all_sessions = feishu_sessions + cli_sessions

    seen = set()
    if cur_sid:
        seen.add(cur_sid)
    deduped = []
    for s in all_sessions:
        sid = s["session_id"]
        if sid not in seen:
            seen.add(sid)
            deduped.append(s)

    deduped.sort(key=lambda s: s.get("started_at", ""), reverse=True)
    return deduped[:15]


async def _format_session_list(user_id: str, chat_id: str, store: SessionStore) -> str:
    """生成历史 sessions 列表（去重 + 手机友好格式），含当前 session"""
    from session_store import _clean_preview

    cur = await store.get_current_raw(user_id, chat_id)
    cur_sid = cur.get("session_id")

    cli_all = scan_cli_sessions(30)
    cli_preview_map = {s["session_id"]: s for s in cli_all}

    all_sessions = _build_session_list(user_id, chat_id, store)

    def _fmt_time(raw: str) -> str:
        t = raw[:16].replace("T", " ")
        if len(t) >= 16:
            t = t[5:16].replace("-", "/")
        return t

    # 收集所有需要展示的 session_id
    all_sids = []
    if cur_sid:
        all_sids.append(cur_sid)
    for s in all_sessions:
        all_sids.append(s["session_id"])

    # 懒加载：为缺失摘要的 session 生成（限制 5 个，避免太慢）
    summaries = {}
    missing = []
    for sid in all_sids:
        cached = store.get_summary(user_id, sid)
        if cached:
            summaries[sid] = cached
        else:
            missing.append(sid)

    if missing:
        token = _get_api_token()
        if token:
            new_summaries = {}
            for sid in missing[:5]:
                s = generate_summary(sid, token=token)
                if s:
                    new_summaries[sid] = s
                    summaries[sid] = s
                    _write_custom_title(sid, s)
            if new_summaries:
                await store.batch_set_summaries(user_id, new_summaries)

    lines = []

    def _strip_md(text: str) -> str:
        """去除 markdown 格式 + 压成单行纯文本"""
        # 换行 → 空格，压成单行
        text = " ".join(text.split())
        # heading 标记
        while text.startswith("#"):
            text = text.lstrip("#").lstrip()
        # bold / italic
        text = text.replace("**", "").replace("__", "")
        # backtick
        text = text.replace("`", "")
        # XML 残留标签名（如 <tool_call>）
        text = text.replace("<", "").replace(">", "")
        return text.strip()

    def _desc(sid: str, preview_raw: str) -> str:
        """用 summary 优先，没有就用 preview，拼成简短描述"""
        s = summaries.get(sid, "")
        if s:
            s = _strip_md(s)
            return s if len(s) <= 40 else s[:37] + "..."
        p = _clean_preview(preview_raw or "")
        if not p:
            return "（无预览）"
        p = _strip_md(p)
        return p if len(p) <= 40 else p[:37] + "..."

    # 当前 session
    if cur_sid:
        cli_info = cli_preview_map.get(cur_sid)
        preview = (cli_info.get("preview") if cli_info and cli_info.get("preview")
                   else cur.get("preview") or "")
        started = _fmt_time(cur.get("started_at", ""))
        lines.append(f"当前  {_desc(cur_sid, preview)} ({started})  #{cur_sid[:8]}")

    if not cur_sid and not all_sessions:
        return "暂无历史 sessions。"

    for i, s in enumerate(all_sessions, 1):
        sid = s["session_id"]
        preview = s.get("preview", "")
        started = _fmt_time(s.get("started_at", ""))
        lines.append(f"{i}. {_desc(sid, preview)} ({started})  #{sid[:8]}")

    if all_sessions:
        lines.append("")
        lines.append("回复 /resume 序号 恢复")
    return "\n".join(lines)


def _list_skills() -> str:
    """扫描 ~/.claude/plugins 目录，列出所有可用的 slash command skills"""
    skills = []
    if not os.path.isdir(PLUGINS_DIR):
        return "暂无已安装的 skills。"

    for root, dirs, files in os.walk(PLUGINS_DIR):
        if os.path.basename(root) != "commands":
            continue
        for fname in files:
            if not fname.endswith(".md"):
                continue
            name = fname[:-3]
            fpath = os.path.join(root, fname)
            desc = ""
            try:
                with open(fpath, encoding="utf-8") as f:
                    in_frontmatter = False
                    for line in f:
                        line = line.strip()
                        if line == "---" and not in_frontmatter:
                            in_frontmatter = True
                            continue
                        if line == "---" and in_frontmatter:
                            break
                        if in_frontmatter and line.startswith("description:"):
                            desc = line[len("description:"):].strip().strip('"')
            except OSError:
                pass
            skills.append((name, desc))

    if not skills:
        return "暂无已安装的 skills。"

    skills.sort(key=lambda x: x[0])
    lines = ["🛠 **可用 Skills**（发送 `/名称` 即可调用）\n"]
    for name, desc in skills:
        desc_str = f" — {desc}" if desc else ""
        lines.append(f"• `/{name}`{desc_str}")
    return "\n".join(lines)


def _get_usage() -> str:
    """
    发一个轻量 API 请求，从响应 headers 获取 Claude Max 订阅用量百分比和重置时间。
    """
    if sys.platform != "darwin":
        return "❌ /usage 目前只支持 macOS"

    import urllib.request
    import urllib.error
    import ssl

    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        creds = json.loads(result.stdout.strip())
        token = creds["claudeAiOauth"]["accessToken"]
    except Exception as e:
        return f"❌ 读取凭证失败：{e}"

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            headers = dict(resp.headers)
    except urllib.error.HTTPError as e:
        headers = dict(e.headers)
    except Exception as e:
        return f"❌ 获取用量失败：{e}"

    def h(key):
        return headers.get(key) or headers.get(key.lower()) or headers.get(key.replace("-", "_"))

    def fmt_pct(val):
        if val is None:
            return "未知"
        pct = float(val) * 100
        bar_len = 20
        filled = round(pct / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        return f"{bar} {pct:.1f}%"

    def fmt_reset(ts):
        if ts is None:
            return "未知"
        try:
            dt = datetime.fromtimestamp(int(ts))
            now = datetime.now()
            diff = dt - now
            hours = int(diff.total_seconds() // 3600)
            minutes = int((diff.total_seconds() % 3600) // 60)
            return f"{dt.strftime('%m/%d %H:%M')}（{hours}h{minutes}m 后）"
        except Exception:
            return ts

    u5h = h("anthropic-ratelimit-unified-5h-utilization")
    u7d = h("anthropic-ratelimit-unified-7d-utilization")
    r5h = h("anthropic-ratelimit-unified-5h-reset")
    r7d = h("anthropic-ratelimit-unified-7d-reset")
    s5h = h("anthropic-ratelimit-unified-5h-status") or "unknown"
    s7d = h("anthropic-ratelimit-unified-7d-status") or "unknown"

    if u5h is None and u7d is None:
        return "📊 **Usage**\n\n未能获取用量数据（响应中无用量 headers）。"

    lines = ["📊 **Claude Max 用量**\n"]
    lines.append(f"**5小时窗口**（状态：{s5h}）")
    lines.append(f"{fmt_pct(u5h)}")
    lines.append(f"重置时间：{fmt_reset(r5h)}\n")
    lines.append(f"**7天窗口**（状态：{s7d}）")
    lines.append(f"{fmt_pct(u7d)}")
    lines.append(f"重置时间：{fmt_reset(r7d)}")

    return "\n".join(lines)



def _list_mcp() -> str:
    """调用 claude mcp list 获取已配置的 MCP servers"""
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "mcp", "list"],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout.strip()
    except Exception as e:
        return f"❌ 获取 MCP 列表失败：{e}"

    if not output:
        return "暂无已配置的 MCP servers。\n\n用 `claude mcp add` 在终端添加。"

    return f"🔌 **已配置的 MCP Servers**\n\n{output}"


async def _list_directory(user_id: str, chat_id: str, store: SessionStore, args: str) -> str:
    cur = await store.get_current_raw(user_id, chat_id)
    base_dir = cur.get("cwd", DEFAULT_CWD)
    raw_target = args.strip()

    if not raw_target:
        target = base_dir
        display_target = "."
    elif os.path.isabs(raw_target):
        target = os.path.expanduser(raw_target)
        display_target = target
    else:
        target = os.path.abspath(os.path.join(base_dir, os.path.expanduser(raw_target)))
        display_target = raw_target

    if not os.path.exists(target):
        return f"❌ 路径不存在：`{display_target}`\n当前工作目录：`{base_dir}`"

    if not os.path.isdir(target):
        return f"❌ 目标不是目录：`{display_target}`"

    try:
        entries = []
        with os.scandir(target) as it:
            for entry in it:
                suffix = "/" if entry.is_dir() else ""
                entries.append((not entry.is_dir(), entry.name.lower(), f"`{entry.name}{suffix}`"))
    except OSError as e:
        return f"❌ 读取目录失败：{e}"

    entries.sort()
    preview = [item[2] for item in entries[:50]]
    hidden_count = max(0, len(entries) - len(preview))

    lines = [
        "📁 **目录内容**",
        f"请求路径：`{display_target}`",
        f"绝对路径：`{target}`",
    ]
    if not preview:
        lines.append("（空目录）")
        return "\n".join(lines)

    lines.append("")
    lines.extend(preview)
    if hidden_count:
        lines.append("")
        lines.append(f"…… 还有 {hidden_count} 项未显示")
    return "\n".join(lines)


async def _format_workspace_list(user_id: str, chat_id: str, store: SessionStore) -> str:
    cur = await store.get_current_raw(user_id, chat_id)
    current_name = cur.get("workspace", "")
    current_cwd = cur.get("cwd", "~")
    workspaces = store.list_workspaces(user_id)

    lines = ["🗂 **工作空间**"]
    lines.append(f"当前绑定：`{current_name}`" if current_name else "当前绑定：（未命名）")
    lines.append(f"当前目录：`{current_cwd}`")

    if workspaces:
        lines.append("")
        lines.append("已保存：")
        for name, path in workspaces.items():
            marker = " ← 当前群组" if name == current_name else ""
            lines.append(f"• `{name}` → `{path}`{marker}")
    else:
        lines.append("")
        lines.append("还没有已保存的工作空间。")

    lines.append("")
    lines.append("用法：")
    lines.append("`/ws save 名称 [路径]` 保存工作空间")
    lines.append("`/ws use 名称` 绑定当前群组到该工作空间")
    lines.append("`/ws set 路径` 直接设置当前群组目录")
    lines.append("`/ws remove 名称` 删除已保存的工作空间")
    return "\n".join(lines)


async def _handle_workspace_command(
    args: str,
    user_id: str,
    chat_id: str,
    store: SessionStore,
) -> str:
    if not args:
        return await _format_workspace_list(user_id, chat_id, store)

    try:
        parts = shlex.split(args)
    except ValueError as e:
        return f"❌ 参数解析失败：{e}"

    if not parts:
        return await _format_workspace_list(user_id, chat_id, store)

    action = parts[0].lower()

    if action in {"list", "ls"}:
        return await _format_workspace_list(user_id, chat_id, store)

    if action in {"save", "add"}:
        if len(parts) < 2:
            return "⚠️ 用法：`/ws save 名称 [路径]`"
        name = parts[1]
        path = (await store.get_current_raw(user_id, chat_id)).get("cwd", DEFAULT_CWD)
        if len(parts) >= 3:
            path = os.path.expanduser(parts[2])
        if not os.path.isdir(path):
            return f"❌ 路径不存在：`{path}`"
        await store.save_workspace(user_id, name, path)
        return f"✅ 已保存工作空间 `{name}` → `{path}`"

    if action == "use":
        if len(parts) != 2:
            return "⚠️ 用法：`/ws use 名称`"
        name = parts[1]
        path = await store.bind_workspace(user_id, chat_id, name)
        if not path:
            return f"❌ 未找到工作空间：`{name}`，先用 `/ws save {name} 路径` 保存。"
        return (
            f"✅ 当前群组已绑定工作空间 `{name}`\n"
            f"工作目录：`{path}`\n"
            "如需清空旧上下文，可继续发送 `/new`。"
        )

    if action == "set":
        if len(parts) != 2:
            return "⚠️ 用法：`/ws set 路径`"
        path = os.path.expanduser(parts[1])
        if not os.path.isdir(path):
            return f"❌ 路径不存在：`{path}`"
        old_name = (await store.get_current_raw(user_id, chat_id)).get("workspace", "")
        await store.set_cwd(user_id, chat_id, path)
        suffix = "，并解除原工作空间绑定" if old_name else ""
        return f"✅ 当前群组工作目录已切换为 `{path}`{suffix}"

    if action in {"remove", "delete", "rm"}:
        if len(parts) != 2:
            return "⚠️ 用法：`/ws remove 名称`"
        name = parts[1]
        if not await store.delete_workspace(user_id, name):
            return f"❌ 未找到工作空间：`{name}`"
        return f"✅ 已删除工作空间 `{name}`"

    return (
        f"❌ 未知子命令：`{action}`\n"
        "可用：`list`、`save`、`use`、`set`、`remove`"
    )


async def handle_command(
    cmd: str,
    args: str,
    user_id: str,
    chat_id: str,
    store: SessionStore,
) -> Optional[str]:
    """处理命令，返回回复文本。返回 None 表示不是 bot 命令，应转发给 Claude。"""

    if cmd not in BOT_COMMANDS:
        return None  # 不认识的 /xxx → 转发给 Claude（如 /commit 等 skill）

    if cmd == "ws":
        cmd = "workspace"

    if cmd in ("help", "h"):
        return HELP_TEXT

    elif cmd in ("new", "clear"):
        old_title = await store.new_session(user_id, chat_id)
        if old_title:
            return f"✅ 已开始新 session。\n上个会话：「{old_title}」"
        return "✅ 已开始新 session，之前的对话历史已清除。"

    elif cmd == "resume":
        if not args:
            return await _format_session_list(user_id, chat_id, store)
        # 如果是数字序号，先在合并列表中找到对应 session_id
        try:
            idx = int(args) - 1
            all_sessions = await _build_session_list(user_id, chat_id, store)
            if 0 <= idx < len(all_sessions):
                args = all_sessions[idx]["session_id"]
            else:
                return f"❌ 序号 {int(args)} 超出范围（共 {len(all_sessions)} 条）。"
        except ValueError:
            pass  # 直接用 session ID 字符串
        session_id, old_title = await store.resume_session(user_id, chat_id, args)
        if not session_id:
            return f"❌ 未找到 session：`{args}`，用 `/resume` 查看列表。"
        reply = f"✅ 已恢复 session `{session_id[:8]}...`，继续对话吧。"
        if old_title:
            reply += f"\n上个会话：「{old_title}」"
        return reply

    elif cmd == "model":
        if not args:
            cur = await store.get_current(user_id, chat_id)
            return f"当前模型：`{cur.model}`\n可用：opus / sonnet / haiku 或完整模型 ID"
        model = MODEL_ALIASES.get(args.lower(), args)
        await store.set_model(user_id, chat_id, model)
        return f"✅ 已切换模型为 `{model}`"

    elif cmd == "status":
        cur = await store.get_current_raw(user_id, chat_id)
        sid = cur.get("session_id") or "（新 session）"
        model = cur.get("model", "未知")
        cwd = cur.get("cwd", "~")
        workspace = cur.get("workspace") or "（未绑定）"
        started = cur.get("started_at", "")[:16].replace("T", " ")
        mode = cur.get("permission_mode") or "bypassPermissions"
        return (
            f"📊 **当前 Session 状态**\n"
            f"Session ID: `{sid}`\n"
            f"模型: `{model}`\n"
            f"权限模式: `{mode}`\n"
            f"工作空间: `{workspace}`\n"
            f"工作目录: `{cwd}`\n"
            f"开始时间: {started}"
        )

    elif cmd == "mode":
        if not args:
            cur = await store.get_current(user_id, chat_id)
            current_mode = cur.permission_mode
            lines = [f"当前模式：**{current_mode}** — {VALID_MODES.get(current_mode, '')}\n"]
            lines.append("**可选模式：**")
            for mode, desc in VALID_MODES.items():
                marker = " ← 当前" if mode == current_mode else ""
                lines.append(f"• `{mode}` — {desc}{marker}")
            lines.append("\n用 `/mode [模式名]` 切换。")
            return "\n".join(lines)
        mode = MODE_ALIASES.get(args.lower(), args)
        if mode not in VALID_MODES:
            return f"❌ 未知模式：`{args}`\n可选：{', '.join(f'`{m}`' for m in VALID_MODES)}"
        await store.set_permission_mode(user_id, chat_id, mode)
        return f"✅ 已切换为 **{mode}** — {VALID_MODES[mode]}"

    elif cmd == "cd":
        if not args:
            return "⚠️ 用法：`/cd [路径]`"
        path = os.path.expanduser(args)
        if not os.path.isdir(path):
            return f"❌ 路径不存在：`{path}`"
        old_name = (await store.get_current_raw(user_id, chat_id)).get("workspace", "")
        await store.set_cwd(user_id, chat_id, path)
        suffix = "，并解除原工作空间绑定" if old_name else ""
        return f"✅ 工作目录已切换为 `{path}`{suffix}"

    elif cmd == "ls":
        return await _list_directory(user_id, chat_id, store, args)

    elif cmd == "workspace":
        return await _handle_workspace_command(args, user_id, chat_id, store)

    elif cmd == "skills":
        return _list_skills()

    elif cmd == "mcp":
        return _list_mcp()

    elif cmd == "usage":
        return _get_usage()

    else:
        return None  # fallback: 转发给 Claude
