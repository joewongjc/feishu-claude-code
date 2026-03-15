import asyncio
import json
import os
import subprocess
import ssl
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional

from bot_config import SESSIONS_DIR, DEFAULT_MODEL, DEFAULT_CWD, PERMISSION_MODE

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def scan_cli_sessions(limit: int = 30) -> list[dict]:
    """
    扫描 ~/.claude/projects/ 下所有 session .jsonl 文件。
    返回列表，每项：{session_id, started_at, cwd, preview, source="terminal"}
    按最近修改时间倒序，最多返回 limit 条。
    """
    results = []
    if not os.path.isdir(CLAUDE_PROJECTS_DIR):
        return results

    for project_dir in os.listdir(CLAUDE_PROJECTS_DIR):
        project_path = os.path.join(CLAUDE_PROJECTS_DIR, project_dir)
        if not os.path.isdir(project_path):
            continue
        for fname in os.listdir(project_path):
            if not fname.endswith(".jsonl"):
                continue
            session_id = fname[:-6]  # 去掉 .jsonl
            fpath = os.path.join(project_path, fname)
            mtime = os.path.getmtime(fpath)
            results.append((mtime, session_id, fpath))

    # 按最近修改时间倒序
    results.sort(key=lambda x: x[0], reverse=True)
    results = results[:limit]

    sessions = []
    for mtime, session_id, fpath in results:
        info = _parse_session_file(fpath, session_id, mtime)
        sessions.append(info)
    return sessions


import re

def _clean_preview(text: str) -> str:
    """清洗 preview 文本，去掉系统注入内容"""
    # 去掉 [环境：...] 前缀
    text = re.sub(r'^\[环境：[^\]]*\]\s*', '', text)
    # 去掉 <local-command-caveat>...</local-command-caveat> 及其后的系统文本
    text = re.sub(r'<local-command-caveat>.*?</local-command-caveat>\s*', '', text, flags=re.DOTALL)
    # 去掉 <system-reminder>...</system-reminder>
    text = re.sub(r'<system-reminder>.*?</system-reminder>\s*', '', text, flags=re.DOTALL)
    # 去掉其他 XML-like 系统标签
    text = re.sub(r'<[a-z_-]+>.*?</[a-z_-]+>\s*', '', text, flags=re.DOTALL)
    return text.strip()


def _parse_session_file(fpath: str, session_id: str, mtime: float) -> dict:
    """从 .jsonl 文件提取首条用户消息（作为 preview）、cwd、时间戳"""
    preview = ""
    cwd = ""
    started_at = datetime.fromtimestamp(mtime).isoformat()

    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "user":
                    continue
                # 取 cwd
                if not cwd and d.get("cwd"):
                    cwd = d["cwd"]
                # 取 timestamp
                if d.get("timestamp"):
                    started_at = d["timestamp"][:19].replace("T", " ")
                # 取用户消息文本
                msg = d.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content if b.get("type") == "text"
                    ).strip()
                else:
                    text = str(content).strip()
                if text:
                    text = _clean_preview(text)
                    if text:
                        preview = text[:50]
                        break
    except OSError:
        pass

    return {
        "session_id": session_id,
        "started_at": started_at,
        "cwd": cwd,
        "preview": preview,
        "source": "terminal",
    }

def _find_session_file(session_id: str) -> Optional[str]:
    """在 ~/.claude/projects/ 下找到 session 对应的 .jsonl 文件"""
    if not os.path.isdir(CLAUDE_PROJECTS_DIR):
        return None
    for project_dir in os.listdir(CLAUDE_PROJECTS_DIR):
        project_path = os.path.join(CLAUDE_PROJECTS_DIR, project_dir)
        if not os.path.isdir(project_path):
            continue
        fpath = os.path.join(project_path, f"{session_id}.jsonl")
        if os.path.isfile(fpath):
            return fpath
    return None


def _extract_conversation_context(fpath: str, max_chars: int = 2000) -> str:
    """从 .jsonl 提取前几轮对话文本，用于生成摘要"""
    parts = []
    total = 0
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if d.get("type") not in ("user", "assistant"):
                    continue
                if d.get("isMeta"):
                    continue
                msg = d.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content
                        if b.get("type") == "text"
                    ).strip()
                else:
                    text = str(content).strip()
                if not text:
                    continue
                text = _clean_preview(text)
                if not text:
                    continue
                role = "用户" if d["type"] == "user" else "助手"
                part = f"{role}: {text}"
                parts.append(part)
                total += len(part)
                if total >= max_chars:
                    break
    except OSError:
        pass
    return "\n".join(parts)


def _get_api_token() -> Optional[str]:
    """获取 Claude API token，先试 credentials 文件，再试 keychain"""
    try:
        creds_path = os.path.expanduser("~/.claude/.credentials.json")
        if os.path.isfile(creds_path):
            with open(creds_path) as f:
                creds = json.load(f)
            return creds["claudeAiOauth"]["accessToken"]
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        creds = json.loads(result.stdout.strip())
        return creds["claudeAiOauth"]["accessToken"]
    except Exception:
        return None


def generate_summary(session_id: str, token: Optional[str] = None) -> str:
    """为指定 session 调用 haiku 生成一句话摘要"""
    fpath = _find_session_file(session_id)
    if not fpath:
        return ""
    context = _extract_conversation_context(fpath)
    if not context:
        return ""
    if token is None:
        token = _get_api_token()
    if not token:
        return ""

    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 80,
        "messages": [{"role": "user", "content": (
            "请用一句话（15-25个字）总结以下对话的主题。"
            "只返回摘要文本，不要加引号或其他格式。\n\n"
            + context[:2000]
        )}],
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
            result = json.loads(resp.read())
            blocks = result.get("content", [])
            if blocks and blocks[0].get("type") == "text":
                return blocks[0]["text"].strip()
    except Exception:
        pass
    return ""


def _write_custom_title(session_id: str, title: str):
    """将摘要作为 custom-title 写入 .jsonl，让 CLI 终端也能显示"""
    fpath = _find_session_file(session_id)
    if not fpath:
        return
    # 检查是否已有 custom-title 行，幂等
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "custom-title":
                    return  # 已存在，跳过
    except OSError:
        return
    # 追加 custom-title 行
    entry = json.dumps({
        "type": "custom-title",
        "customTitle": title,
        "sessionId": session_id,
    }, ensure_ascii=False)
    try:
        with open(fpath, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except OSError:
        pass


SESSIONS_FILE = os.path.join(SESSIONS_DIR, "sessions.json")


class Session:
    def __init__(
        self,
        session_id: Optional[str],
        model: str,
        cwd: str,
        permission_mode: str,
        workspace: str = "",
    ):
        self.session_id = session_id
        self.model = model
        self.cwd = cwd
        self.permission_mode = permission_mode
        self.workspace = workspace


class SessionStore:
    def __init__(self):
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        self._save_lock = asyncio.Lock()  # 保护 _save() 的全局锁
        self._data: dict = self._load()
        self._dedup_all_histories()

    def _load(self) -> dict:
        if os.path.exists(SESSIONS_FILE):
            try:
                with open(SESSIONS_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save(self):
        with open(SESSIONS_FILE, "w") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    async def _save_async(self):
        """异步保存，使用锁保护并发写入"""
        async with self._save_lock:
            with open(SESSIONS_FILE, "w") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)

    def _dedup_all_histories(self):
        """启动时清理所有用户 history 中的重复 session_id"""
        changed = False
        for user in self._data.values():
            for chat_data in user.values():
                if not isinstance(chat_data, dict) or "history" not in chat_data:
                    continue
                history = chat_data.get("history", [])
                seen = set()
                cleaned = []
                # 倒序遍历，保留每个 session_id 最后出现的那条
                for h in reversed(history):
                    sid = h.get("session_id")
                    if sid and sid not in seen:
                        seen.add(sid)
                        cleaned.append(h)
                cleaned.reverse()
                if len(cleaned) != len(history):
                    chat_data["history"] = cleaned
                    changed = True
        if changed:
            asyncio.run(self._save_async())

    def _user(self, user_id: str) -> dict:
        return self._data.setdefault(user_id, {})

    def _default_current(self) -> dict:
        return {
            "session_id": None,
            "model": DEFAULT_MODEL,
            "cwd": DEFAULT_CWD,
            "permission_mode": PERMISSION_MODE,
            "started_at": datetime.now().isoformat(),
            "preview": "",
            "workspace": "",
        }

    def _normalize_chat_key(self, user_id: str, chat_id: str) -> str:
        return "private" if chat_id == user_id else chat_id

    def _ensure_current_defaults(self, current: dict) -> bool:
        changed = False
        defaults = self._default_current()
        for key, value in defaults.items():
            if key not in current:
                current[key] = value
                changed = True
        return changed

    async def _ensure_chat_data(self, user_id: str, chat_id: str) -> dict:
        user = self._user(user_id)
        chat_key = self._normalize_chat_key(user_id, chat_id)
        changed = False

        if chat_key not in user:
            # 兼容旧结构：首次访问私聊时把顶层 current/history 迁入 private。
            if chat_key == "private" and isinstance(user.get("current"), dict):
                user[chat_key] = {
                    "current": user.pop("current"),
                    "history": user.pop("history", []),
                }
            else:
                user[chat_key] = {
                    "current": self._default_current(),
                    "history": [],
                }
            changed = True

        chat_data = user[chat_key]
        if self._ensure_current_defaults(chat_data.setdefault("current", self._default_current())):
            changed = True
        if "history" not in chat_data:
            chat_data["history"] = []
            changed = True

        if changed:
            await self._save_async()

        return chat_data

    def get_summary(self, user_id: str, session_id: str) -> str:
        """获取缓存的摘要"""
        return self._user(user_id).get("summaries", {}).get(session_id, "")

    async def batch_set_summaries(self, user_id: str, summaries: dict):
        """批量缓存摘要并保存"""
        user = self._user(user_id)
        user.setdefault("summaries", {}).update(summaries)
        await self._save_async()

    async def get_current(self, user_id: str, chat_id: str) -> Session:
        """Get current session config for a specific chat"""
        cur = await self.get_current_raw(user_id, chat_id)
        return Session(
            session_id=cur.get("session_id"),
            model=cur.get("model", DEFAULT_MODEL),
            cwd=cur.get("cwd", DEFAULT_CWD),
            permission_mode=cur.get("permission_mode", PERMISSION_MODE),
            workspace=cur.get("workspace", ""),
        )

    async def on_claude_response(self, user_id: str, chat_id: str, new_session_id: str, first_message: str):
        """Claude 回复后用返回的 session_id 更新状态"""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        cur = chat_data["current"]
        old_id = cur.get("session_id")

        if old_id and old_id != new_session_id:
            # 归档旧 session（先去重，避免同一 session_id 重复出现）
            chat_data["history"] = [h for h in chat_data["history"] if h["session_id"] != old_id]
            chat_data["history"].append({
                "session_id": old_id,
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            chat_data["history"] = chat_data["history"][-20:]
            cur["started_at"] = datetime.now().isoformat()
            # 为归档的 session 生成摘要（best-effort）
            summaries = self._data[user_id].get("summaries", {})
            if not summaries.get(old_id):
                try:
                    summary = generate_summary(old_id)
                    if summary:
                        self._data[user_id].setdefault("summaries", {})[old_id] = summary
                        _write_custom_title(old_id, summary)
                except Exception:
                    pass

        cur["session_id"] = new_session_id
        if not cur.get("preview"):
            cur["preview"] = _clean_preview(first_message)[:40]
        await self._save_async()

    async def new_session(self, user_id: str, chat_id: str) -> str:
        """Start a new session for a specific chat, return old session title"""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        cur = chat_data["current"]
        old_title = ""

        if cur.get("session_id"):
            old_id = cur["session_id"]
            # Archive current session (dedup first)
            chat_data["history"] = [h for h in chat_data.get("history", []) if h["session_id"] != old_id]
            chat_data["history"].append({
                "session_id": old_id,
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            chat_data["history"] = chat_data["history"][-20:]

            # Get summary: prefer cached, otherwise generate
            summaries = self._data[user_id].get("summaries", {})
            old_title = summaries.get(old_id, "")
            if not old_title:
                try:
                    old_title = generate_summary(old_id)
                    if old_title:
                        self._data[user_id].setdefault("summaries", {})[old_id] = old_title
                        _write_custom_title(old_id, old_title)
                except Exception:
                    old_title = ""

        # Create new session
        chat_data["current"] = {
            "session_id": None,
            "model": cur.get("model", DEFAULT_MODEL),
            "cwd": cur.get("cwd", DEFAULT_CWD),
            "permission_mode": cur.get("permission_mode", PERMISSION_MODE),
            "started_at": datetime.now().isoformat(),
            "preview": "",
            "workspace": cur.get("workspace", ""),
        }
        await self._save_async()
        return old_title

    async def set_model(self, user_id: str, chat_id: str, model: str):
        """Set model for a specific chat"""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        chat_data["current"]["model"] = model
        await self._save_async()

    async def set_cwd(self, user_id: str, chat_id: str, cwd: str, workspace_name: Optional[str] = None):
        """Set working directory for a specific chat"""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        chat_data["current"]["cwd"] = cwd
        chat_data["current"]["workspace"] = workspace_name or ""
        await self._save_async()

    async def set_permission_mode(self, user_id: str, chat_id: str, mode: str):
        """Set permission mode for a specific chat"""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        chat_data["current"]["permission_mode"] = mode
        await self._save_async()

    async def resume_session(self, user_id: str, chat_id: str, index_or_id: str) -> tuple[Optional[str], str]:
        """按序号（1-based）或 session_id 恢复 session，返回 (session_id, old_title)"""
        if user_id not in self._data:
            return None, ""

        chat_key = self._normalize_chat_key(user_id, chat_id)
        if chat_key not in self._data[user_id]:
            return None, ""

        chat_data = await self._ensure_chat_data(user_id, chat_id)
        history = chat_data.get("history", [])

        try:
            idx = int(index_or_id) - 1
            if 0 <= idx < len(history):
                session_id = history[idx]["session_id"]
            else:
                return None, ""
        except ValueError:
            session_id = index_or_id

        # 归档 outgoing session（如果有且不是同一个）
        cur = chat_data["current"]
        old_id = cur.get("session_id")
        old_title = ""
        if old_id and old_id != session_id:
            chat_data["history"] = [h for h in chat_data["history"] if h["session_id"] != old_id]
            chat_data["history"].append({
                "session_id": old_id,
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            chat_data["history"] = chat_data["history"][-20:]
            # 获取摘要：优先缓存，否则生成
            summaries = self._data[user_id].get("summaries", {})
            old_title = summaries.get(old_id, "")
            if not old_title:
                try:
                    old_title = generate_summary(old_id)
                    if old_title:
                        self._data[user_id].setdefault("summaries", {})[old_id] = old_title
                        _write_custom_title(old_id, old_title)
                except Exception:
                    old_title = ""

        # 从 history 中找回原始 preview 和 started_at
        original_preview = ""
        original_started = ""
        for h in chat_data["history"]:
            if h["session_id"] == session_id:
                original_preview = h.get("preview", "")
                original_started = h.get("started_at", "")
                break
        cur["session_id"] = session_id
        cur["preview"] = original_preview
        cur["started_at"] = original_started or datetime.now().isoformat()
        await self._save_async()
        return session_id, old_title

    async def list_sessions(self, user_id: str, chat_id: str) -> list:
        """List all sessions for a specific chat"""
        if user_id not in self._data:
            return []

        chat_key = self._normalize_chat_key(user_id, chat_id)
        if chat_key not in self._data[user_id]:
            return []

        return list(reversed((await self._ensure_chat_data(user_id, chat_id)).get("history", [])))

    def list_workspaces(self, user_id: str) -> dict[str, str]:
        """List saved workspaces for a user"""
        return dict(sorted(self._user(user_id).get("workspaces", {}).items()))

    async def save_workspace(self, user_id: str, name: str, cwd: str):
        """Save or update a named workspace for a user"""
        user = self._user(user_id)
        user.setdefault("workspaces", {})[name] = cwd
        await self._save_async()

    async def delete_workspace(self, user_id: str, name: str) -> bool:
        """Delete a named workspace and clear active bindings that reference it"""
        user = self._user(user_id)
        workspaces = user.setdefault("workspaces", {})
        if name not in workspaces:
            return False

        del workspaces[name]
        for chat_data in user.values():
            if not isinstance(chat_data, dict) or "current" not in chat_data:
                continue
            if chat_data["current"].get("workspace") == name:
                chat_data["current"]["workspace"] = ""
        await self._save_async()
        return True

    async def bind_workspace(self, user_id: str, chat_id: str, name: str) -> Optional[str]:
        """Bind a saved workspace to the current chat"""
        path = self._user(user_id).get("workspaces", {}).get(name)
        if not path:
            return None
        await self.set_cwd(user_id, chat_id, path, workspace_name=name)
        return path

    async def get_current_raw(self, user_id: str, chat_id: str = None) -> dict:
        """Get raw current session data for a specific chat"""
        if chat_id is None:
            chat_id = user_id

        return (await self._ensure_chat_data(user_id, chat_id))["current"]
