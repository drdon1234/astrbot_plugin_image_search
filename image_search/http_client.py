"""普通 HTTP 流式读取辅助函数。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Mapping

import httpx

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 20
_READ_CHUNK_BYTES = 16 * 1024


class ResponseTooLargeError(ValueError):
    """响应声明或实际传输的数据超过调用方上限。"""


class RedirectError(ValueError):
    """重定向缺少目标、形成循环或超过允许次数。"""


@dataclass(frozen=True, slots=True)
class LimitedResponse:
    """受限读取后的必要响应信息。"""

    content: bytes
    url: str
    status_code: int
    headers: httpx.Headers
    encoding: str


def _content_length(headers: httpx.Headers) -> int | None:
    value = headers.get("Content-Length")
    if value is None:
        return None
    try:
        length = int(value, 10)
    except ValueError:
        return None
    return length if length >= 0 else None


async def _fetch_limited_impl(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    headers: Mapping[str, str] | None = None,
    truncate: bool = False,
    max_redirects: int = _MAX_REDIRECTS,
) -> LimitedResponse:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be greater than zero")
    if max_redirects < 0:
        raise ValueError("max_redirects cannot be negative")

    current = url
    visited: set[str] = set()
    for redirect_count in range(max_redirects + 1):
        if current in visited:
            raise RedirectError("URL redirect loop")
        visited.add(current)

        async with client.stream(
            "GET", current, headers=headers, follow_redirects=False
        ) as response:
            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if not location:
                    raise RedirectError("URL redirect is missing Location")
                if redirect_count >= max_redirects:
                    raise RedirectError("too many URL redirects")
                current = str(response.url.join(location))
                continue

            response.raise_for_status()
            declared = _content_length(response.headers)
            if not truncate and declared is not None and declared > max_bytes:
                raise ResponseTooLargeError(
                    f"response Content-Length exceeds {max_bytes} bytes"
                )

            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes(chunk_size=_READ_CHUNK_BYTES):
                if not chunk:
                    continue
                remaining = max_bytes - received
                if len(chunk) > remaining:
                    if not truncate:
                        raise ResponseTooLargeError(
                            f"response body exceeds {max_bytes} bytes"
                        )
                    chunks.append(chunk[:remaining])
                    received = max_bytes
                    break
                chunks.append(chunk)
                received += len(chunk)
                if truncate and received >= max_bytes:
                    break

            return LimitedResponse(
                content=b"".join(chunks),
                url=str(response.url),
                status_code=response.status_code,
                headers=httpx.Headers(response.headers),
                encoding=response.encoding or "utf-8",
            )

    raise RedirectError("too many URL redirects")


async def fetch_limited(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    total_timeout_seconds: float,
    headers: Mapping[str, str] | None = None,
    truncate: bool = False,
    max_redirects: int = _MAX_REDIRECTS,
) -> LimitedResponse:
    """在一个总截止时间内跟随重定向并限制最终响应读取量。"""
    if total_timeout_seconds <= 0:
        raise ValueError("total_timeout_seconds must be greater than zero")
    operation = _fetch_limited_impl(
        client,
        url,
        max_bytes=max_bytes,
        headers=headers,
        truncate=truncate,
        max_redirects=max_redirects,
    )
    return await asyncio.wait_for(operation, timeout=total_timeout_seconds)


__all__ = [
    "LimitedResponse",
    "RedirectError",
    "ResponseTooLargeError",
    "fetch_limited",
]
