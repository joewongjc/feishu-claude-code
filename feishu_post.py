"""
飞书/Lark post（富文本）消息解析：提取纯文本与图片 image_keys。

参考 codex-tg 项目实现，适配多种 post 包装：
  1) {"zh_cn": {...}}
  2) {"post": {"zh_cn": {...}}}
  3) {"title": "...", "content": [...]}
"""

import json
from typing import Any


def _flatten_post_block(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        if not node:
            return ""
        # content 通常是 [[block, block, ...], [block, ...]]（行列表）
        if all(isinstance(x, list) for x in node):
            lines = []
            for line in node:
                line_text = "".join(_flatten_post_block(p) for p in line).strip()
                if line_text:
                    lines.append(line_text)
            return "\n".join(lines)
        return "".join(_flatten_post_block(x) for x in node)
    if isinstance(node, dict):
        tag = str(node.get("tag") or "").lower()
        if tag == "text":
            return str(node.get("text") or "")
        if tag == "a":
            return str(node.get("text") or node.get("href") or "")
        if tag == "at":
            return str(node.get("user_name") or node.get("name") or "")
        if tag in ("img", "media"):
            return "[图片]"
        return "".join(_flatten_post_block(v) for v in node.values())
    return ""


def parse_post_content(raw: str | None) -> str:
    """提取 post 消息的纯文本（title + 扁平化 content）"""
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict):
        return ""

    locale_payload: dict[str, Any] = {}
    roots: list[dict[str, Any]] = [parsed]
    for key in ("post", "data"):
        nested = parsed.get(key)
        if isinstance(nested, dict):
            roots.append(nested)

    for root in roots:
        for loc in ("zh_cn", "en_us", "ja_jp"):
            if isinstance(root.get(loc), dict):
                locale_payload = root[loc]
                break
        if locale_payload:
            break
        if "content" in root or "title" in root:
            locale_payload = root
            break

    if not locale_payload:
        return ""

    title = str(locale_payload.get("title") or "").strip()
    content_node = locale_payload.get("content")
    if isinstance(content_node, str):
        try:
            content_node = json.loads(content_node)
        except json.JSONDecodeError:
            pass

    body = _flatten_post_block(content_node).strip()
    if not body:
        body = _flatten_post_block(locale_payload).strip()

    if title and body:
        return f"{title}\n{body}"
    return title or body


def _collect_image_keys(node: Any, keys: list[str]) -> None:
    if node is None:
        return
    if isinstance(node, list):
        for item in node:
            _collect_image_keys(item, keys)
        return
    if isinstance(node, dict):
        tag = str(node.get("tag") or "").lower()
        if tag in ("img", "media"):
            key = (node.get("image_key") or "").strip()
            if key:
                keys.append(key)
            return
        for v in node.values():
            if isinstance(v, (list, dict)):
                _collect_image_keys(v, keys)


def extract_post_image_keys(raw: str | None) -> list[str]:
    """递归收集 post 内的所有 image_key（按出现顺序，去重保留首次）"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    keys: list[str] = []
    _collect_image_keys(parsed, keys)
    seen = set()
    result = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result
