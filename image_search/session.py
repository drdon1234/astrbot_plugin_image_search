"""Lens 的 HTTP 会话。

上传和 ``qfmetadata`` 必须在**同一个会话**里完成：上传响应会下发一批
cookie，qfmetadata 依赖它们才能返回内容 —— 换一个新的客户端去查，
Google 只会回一个 136 字节的空壳。所以这里把两步封装在一个类里，共享同一个
连接与 cookie jar。

支持两种传输：
* ``httpx``：纯 HTTP，快，不需要浏览器。
* 浏览器的 ``APIRequestContext``：和渲染结果页共用 cookie，更不容易被风控。
"""

from __future__ import annotations

from typing import Any

from .config import SOCS_COOKIE, SearchConfig
from .exceptions import FetchError, UploadError
from .logger import quiet_http_logs
from .metadata import QF_METADATA_URL, build_metadata_params, parse_ocr_lines
from .uploader import UPLOAD_URL, build_upload_params

_REDIRECT_CODES = (301, 302, 303, 307, 308)


class LensSession:
    """一次 Lens 交互的会话容器。"""

    def __init__(self, config: SearchConfig,
                 browser_context: Any | None = None) -> None:
        self._config = config
        self._browser_context = browser_context
        self._client: Any = None

    # -- httpx 客户端（懒创建） -------------------------------------------
    async def _http_client(self) -> Any:
        if self._client is None:
            import httpx

            quiet_http_logs()
            self._client = httpx.AsyncClient(
                timeout=self._config.timeout_ms / 1000,
                follow_redirects=False,
                proxy=self._config.proxy,
                headers={
                    "User-Agent": self._config.user_agent,
                    "Accept-Language": f"{self._config.hl}-US,{self._config.hl};q=0.9",
                },
            )
            self._client.cookies.set("SOCS", SOCS_COOKIE, domain=".google.com")
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> LensSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- 上传 --------------------------------------------------------------
    async def upload(self, image: bytes, filename: str = "image.png",
                     mime: str = "image/png") -> str:
        """上传图片，返回结果页地址（Location 原样）。

        传了 ``browser_context`` 时优先走浏览器的网络栈 —— ``vsrid`` 会话绑定
        在上传方的 cookie 上，只有这样浏览器才能打开对应的结果页。
        """
        params = build_upload_params(self._config.hl)
        headers = {
            "Origin": "https://lens.google.com",
            "Referer": "https://lens.google.com/",
        }

        if self._browser_context is not None:
            location = await self._upload_via_browser(image, filename, mime,
                                                      params, headers)
            if location:
                return location
            # 浏览器传输拿不到 Location 时退回 httpx

        client = await self._http_client()
        try:
            resp = await client.post(
                UPLOAD_URL, params=params,
                files={"encoded_image": (filename, image, mime)},
                headers={**headers,
                         "Accept": "text/html,application/xhtml+xml,"
                                   "application/xml;q=0.9,*/*;q=0.8"},
            )
        except Exception as exc:  # noqa: BLE001
            raise UploadError(f"上传请求失败: {type(exc).__name__}: {exc}") from exc

        location = resp.headers.get("location", "")
        if resp.status_code not in _REDIRECT_CODES or not location:
            raise UploadError(
                f"Lens 上传未返回重定向（status={resp.status_code}）。"
                "接口可能已变更，或图片被拒绝。")
        return location

    async def _upload_via_browser(self, image: bytes, filename: str, mime: str,
                                  params: dict[str, str],
                                  headers: dict[str, str]) -> str | None:
        try:
            resp = await self._browser_context.request.post(
                UPLOAD_URL, params=params, headers=headers,
                multipart={"encoded_image": {"name": filename,
                                             "mimeType": mime,
                                             "buffer": image}},
                max_redirects=0, timeout=self._config.timeout_ms,
            )
        except Exception:  # noqa: BLE001
            return None
        return resp.headers.get("location") or None

    # -- OCR ---------------------------------------------------------------
    async def ocr_lines(self, location: str) -> list[str]:
        """拉 qfmetadata 并还原 OCR 文本行。必须和 upload 用同一个会话。"""
        params = build_metadata_params(location, self._config.hl)
        headers = {"Referer": "https://lens.google.com/"}

        if self._browser_context is not None:
            try:
                resp = await self._browser_context.request.get(
                    QF_METADATA_URL, params=params, headers=headers,
                    timeout=self._config.timeout_ms)
                if resp.status == 200:
                    return parse_ocr_lines(await resp.text())
            except Exception:  # noqa: BLE001
                pass

        client = await self._http_client()
        try:
            resp = await client.get(QF_METADATA_URL, params=params, headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise FetchError(f"qfmetadata 请求失败: {type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200:
            raise FetchError(f"qfmetadata 返回 {resp.status_code}")
        return parse_ocr_lines(resp.text)
