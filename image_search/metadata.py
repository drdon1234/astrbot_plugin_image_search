"""Lens 附属元数据（OCR 文字）。

``lens.google.com/qfmetadata`` 是纯 HTTP 可用的接口，不需要执行 JavaScript，
也没有被人机验证拦截。它返回图片里识别出的文字和区域坐标。
虽然拿不到 exact matches，但对「图里写了什么」这类需求很有用，
也可以作为搜索失败时的降级信息。
"""

from __future__ import annotations

import json
import urllib.parse as up
from typing import Any

from .exceptions import FetchError

QF_METADATA_URL = "https://lens.google.com/qfmetadata"

# Google 内部 JSON 接口的防 JSON 劫持前缀
_XSSI_PREFIX = ")]}'"


def _strip_xssi(text: str) -> str:
    text = text.lstrip()
    if text.startswith(_XSSI_PREFIX):
        text = text[len(_XSSI_PREFIX):]
    return text.lstrip()


def _is_word_node(node: Any) -> bool:
    """JSPB 里的单词节点形如 ``[[序号], "单词", "分隔符", [坐标...]]``。"""
    return (
        isinstance(node, list)
        and len(node) >= 4
        and isinstance(node[0], list)
        and isinstance(node[1], str)
        and isinstance(node[2], str)
        and isinstance(node[3], list)
    )


def _collect_lines(node: Any, lines: list[str]) -> None:
    if not isinstance(node, list):
        return
    if node and all(_is_word_node(child) for child in node):
        line = "".join(f"{child[1]}{child[2]}" for child in node).strip()
        if line:
            lines.append(line)
        return
    for child in node:
        _collect_lines(child, lines)


def parse_ocr_lines(raw: str) -> list[str]:
    """从 qfmetadata 响应里还原 OCR 文本行。"""
    try:
        data = json.loads(_strip_xssi(raw))
    except json.JSONDecodeError as exc:
        raise FetchError(f"qfmetadata 响应不是合法 JSON: {exc}") from exc
    lines: list[str] = []
    _collect_lines(data, lines)
    # 去重但保持顺序
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            result.append(line)
    return result


def build_metadata_params(location: str, hl: str) -> dict[str, str]:
    """从上传返回的结果页地址里取出 qfmetadata 需要的会话参数。"""
    query = dict(up.parse_qsl(up.urlsplit(location).query))
    params = {"hl": hl}
    for key in ("vsrid", "gsessionid", "lsessionid", "vsint"):
        if query.get(key):
            params[key] = query[key]
    if "vsrid" not in params:
        raise FetchError("结果页地址里没有 vsrid，无法查询 qfmetadata")
    return params
