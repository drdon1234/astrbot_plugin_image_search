"""标题补全（可选，尽力而为）。

Google 在 exact matches 页会把过长的标题在服务端截断成 ``xxx ...``，
完整标题在页面里拿不到（试过加宽窗口到 3440px，截断位置不变）。

想要完整标题只能去抓目标页面的 ``<title>``。但这条路不可靠：实测三个站点
里两个被 Cloudflare 拦（403 / 503）。所以这里做成可选功能，失败就保留
Google 给的截断标题，不抛异常。
"""

from __future__ import annotations

import asyncio
import re

from .config import SearchConfig
from .logger import logger, quiet_http_logs
from .models import ExactMatch

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")
_TRUNCATION_SUFFIXES = ("...", "…")
# 只读前 64KB，<title> 一定在 <head> 里
_MAX_BYTES = 64 * 1024


def is_truncated(content: str) -> bool:
    return content.rstrip().endswith(_TRUNCATION_SUFFIXES)


def truncated_prefix(content: str) -> str:
    """去掉结尾的省略号，得到可用于前缀比对的部分。"""
    text = content.rstrip()
    for suffix in _TRUNCATION_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return _WS_RE.sub(" ", text).strip()


def extract_title(html: str) -> str | None:
    match = _TITLE_RE.search(html)
    if not match:
        return None
    import html as html_module

    title = html_module.unescape(match.group(1))
    title = _WS_RE.sub(" ", title).strip()
    return title or None


async def complete_titles(matches: list[ExactMatch], config: SearchConfig,
                          concurrency: int = 4, timeout: float = 8.0) -> int:
    """给被截断的标题尽量补全，返回成功补全的条数。

    只有当目标页 ``<title>`` 以截断前缀开头时才替换 —— 否则说明页面标题
    和 Google 收录的不是一回事（跳转到首页、反爬拦截页等），保留原值更安全。
    """
    targets = [m for m in matches if is_truncated(m.content)]
    if not targets:
        return 0

    import httpx

    quiet_http_logs()
    semaphore = asyncio.Semaphore(concurrency)
    headers = {
        "User-Agent": config.user_agent,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    }

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 proxy=config.proxy, headers=headers) as client:
        async def fetch(match: ExactMatch) -> bool:
            async with semaphore:
                try:
                    resp = await client.get(match.url)
                    if resp.status_code != 200:
                        return False
                    html = resp.text[:_MAX_BYTES]
                except Exception as exc:  # noqa: BLE001
                    logger.debug("补全标题失败 %s: %s", match.url[:60], exc)
                    return False
            title = extract_title(html)
            if not title:
                return False
            prefix = truncated_prefix(match.content)
            if prefix and title.startswith(prefix) and len(title) > len(prefix):
                match.content = title
                return True
            return False

        results = await asyncio.gather(*(fetch(m) for m in targets))
    return sum(results)
