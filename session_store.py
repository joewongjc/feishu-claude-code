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
    def __init__(self, session_id: Optional[str], model: str, cwd: str, permission_mode: str):
        self.session_id = session_id
        self.model = model
        self.cwd = cwd
        self.permission_mode = permission_mode


class SessionStore:
    def __init__(self):
        os.makedirs(SESSIONS_DIR, exist_ok=True)
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
        tmp = SESSIONS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SESSIONS_FILE)  # 原子操作，崩溃时不会截断原文件

    def _dedup_all_histories(self):
        """启动时清理所有用户 history 中的重复 session_id"""
        changed = False
        for user_id, user in self._data.items():
            history = user.get("history", [])
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
                user["history"] = cleaned
                changed = True
        if changed:
            self._save()

    def _user(self, user_id: str) -> dict:
        return self._data.setdefault(user_id, {
            "current": {
                "session_id": None,
                "model": DEFAULT_MODEL,
                "cwd": DEFAULT_CWD,
                "permission_mode": PERMISSION_MODE,
                "started_at": datetime.now().isoformat(),
                "preview": "",
            },
            "history": [],
        })

    def get_summary(self, user_id: str, session_id: str) -> str:
        """获取缓存的摘要"""
        return self._user(user_id).get("summaries", {}).get(session_id, "")

    def batch_set_summaries(self, user_id: str, summaries: dict):
        """批量缓存摘要并保存"""
        user = self._user(user_id)
        user.setdefault("summaries", {}).update(summaries)
        self._save()

    def get_current(self, user_id: str) -> Session:
        cur = self._user(user_id)["current"]
        return Session(
            session_id=cur.get("session_id"),
            model=cur.get("model", DEFAULT_MODEL),
            cwd=cur.get("cwd", DEFAULT_CWD),
            permission_mode=cur.get("permission_mode", PERMISSION_MODE),
        )

    def on_claude_response(self, user_id: str, new_session_id: str, first_message: str):
        """Claude 回复后用返回的 session_id 更新状态"""
        user = self._user(user_id)
        cur = user["current"]
        old_id = cur.get("session_id")

        if old_id and old_id != new_session_id:
            # 归档旧 session（先去重，避免同一 session_id 重复出现）
            user["history"] = [h for h in user["history"] if h["session_id"] != old_id]
            user["history"].append({
                "session_id": old_id,
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            user["history"] = user["history"][-20:]
            cur["started_at"] = datetime.now().isoformat()
            # 为归档的 session 生成摘要（best-effort）
            if not user.get("summaries", {}).get(old_id):
                try:
                    summary = generate_summary(old_id)
                    if summary:
                        user.setdefault("summaries", {})[old_id] = summary
                        _write_custom_title(old_id, summary)
                except Exception:
                    pass

        cur["session_id"] = new_session_id
        if not cur.get("preview"):
            cur["preview"] = _clean_preview(first_message)[:40]
        self._save()

    def new_session(self, user_id: str) -> str:
        """开始新 session，归档旧的并返回旧 session 的标题（空字符串表示无旧 session）"""
        user = self._user(user_id)
        cur = user["current"]
        old_title = ""
        if cur.get("session_id"):
            old_id = cur["session_id"]
            # 归档当前 session（先去重）
            user["history"] = [h for h in user["history"] if h["session_id"] != old_id]
            user["history"].append({
                "session_id": old_id,
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            user["history"] = user["history"][-20:]
            # 获取摘要：优先缓存，否则生成
            old_title = user.get("summaries", {}).get(old_id, "")
            if not old_title:
                try:
                    old_title = generate_summary(old_id)
                    if old_title:
                        user.setdefault("summaries", {})[old_id] = old_title
                        _write_custom_title(old_id, old_title)
                except Exception:
                    old_title = ""
        user["current"] = {
            "session_id": None,
            "model": cur.get("model", DEFAULT_MODEL),
            "cwd": cur.get("cwd", DEFAULT_CWD),
            "permission_mode": cur.get("permission_mode", PERMISSION_MODE),
            "started_at": datetime.now().isoformat(),
            "preview": "",
        }
        self._save()
        return old_title

    def set_model(self, user_id: str, model: str):
        self._user(user_id)["current"]["model"] = model
        self._save()

    def set_cwd(self, user_id: str, cwd: str):
        self._user(user_id)["current"]["cwd"] = cwd
        self._save()

    def set_permission_mode(self, user_id: str, mode: str):
        self._user(user_id)["current"]["permission_mode"] = mode
        self._save()

    def resume_session(self, user_id: str, index_or_id: str) -> tuple[Optional[str], str]:
        """按序号（1-based）或 session_id 恢复 session，返回 (session_id, old_title)"""
        user = self._user(user_id)
        history = user["history"]

        try:
            idx = int(index_or_id) - 1
            if 0 <= idx < len(history):
                session_id = history[idx]["session_id"]
            else:
                return None, ""
        except ValueError:
            session_id = index_or_id

        # 归档 outgoing session（如果有且不是同一个）
        cur = user["current"]
        old_id = cur.get("session_id")
        old_title = ""
        if old_id and old_id != session_id:
            user["history"] = [h for h in user["history"] if h["session_id"] != old_id]
            user["history"].append({
                "session_id": old_id,
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            user["history"] = user["history"][-20:]
            # 获取摘要：优先缓存，否则生成
            old_title = user.get("summaries", {}).get(old_id, "")
            if not old_title:
                try:
                    old_title = generate_summary(old_id)
                    if old_title:
                        user.setdefault("summaries", {})[old_id] = old_title
                        _write_custom_title(old_id, old_title)
                except Exception:
                    old_title = ""

        # 从 history 中找回原始 preview 和 started_at
        original_preview = ""
        original_started = ""
        for h in user["history"]:
            if h["session_id"] == session_id:
                original_preview = h.get("preview", "")
                original_started = h.get("started_at", "")
                break
        cur["session_id"] = session_id
        cur["preview"] = original_preview
        cur["started_at"] = original_started or datetime.now().isoformat()
        self._save()
        return session_id, old_title

    def list_sessions(self, user_id: str) -> list:
        return list(reversed(self._user(user_id)["history"]))

    def get_current_raw(self, user_id: str) -> dict:
        return self._user(user_id)["current"]
