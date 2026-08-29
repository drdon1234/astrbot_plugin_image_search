"""长驻服务封装：懒启动 + 空闲自动关闭浏览器。

机器人插件的使用特点是「偶发调用、间隔很长」。浏览器常驻能省掉首次 UA 探测
和预热（单次从 23 秒降到 12 秒），但一直挂着又白占几百 MB 内存。所以这里做成
用到才启动、空闲一段时间后自动关闭，下次再用又会自动拉起来。
"""

from __future__ import annotations

import asyncio
import time

from .browser import BrowserSession
from .config import SearchConfig
from .installer import InstallState
from .loader import ImageInput
from .logger import logger
from .models import LensSearchResult
from .searcher import GoogleLensSearcher


class LensSearchService:
    """把 :class:`GoogleLensSearcher` 包成适合常驻进程用的服务。

    Args:
        config: 搜索配置。
        idle_close_seconds: 空闲多少秒后关掉浏览器；``0`` 表示常驻不关。
    """

    def __init__(self, config: SearchConfig,
                 idle_close_seconds: int = 1800) -> None:
        self._config = config
        self._idle_close_seconds = max(0, idle_close_seconds)
        self._searcher: GoogleLensSearcher | None = None
        self._probe_session: BrowserSession | None = None
        self._lock = asyncio.Lock()
        self._last_used = 0.0
        self._idle_task: asyncio.Task[None] | None = None
        self._closing = False

    @property
    def config(self) -> SearchConfig:
        return self._config

    @property
    def running(self) -> bool:
        return self._searcher is not None

    # -- 浏览器就绪状态 ------------------------------------------------------
    def _probe(self) -> BrowserSession:
        """一个只用来查状态 / 装浏览器的会话，不启动浏览器。"""
        if self._probe_session is None:
            self._probe_session = BrowserSession(self._config)
        return self._probe_session

    def browser_ready(self) -> bool:
        """浏览器二进制是否已就位（纯文件检查，不启动进程）。"""
        return self._probe().browser_ready()

    def install_status(self) -> str:
        """给用户看的浏览器状态。

        注意区分两种「就绪」：自动安装装好的，和系统本来就有的
        （比如宿主机装了 Chrome）—— 后者 installer 自己是 IDLE 状态。
        """
        session = self._probe()
        installer = session.installer
        if installer.state is InstallState.IDLE and session.browser_ready():
            return "已有可用浏览器（无需自动安装）"
        return installer.status_text()

    async def prepare(self) -> None:
        """提前把浏览器装好，不启动浏览器。

        适合插件加载后丢到后台跑：AstrBot 装插件时不会执行
        ``playwright install``，与其等用户第一次搜图时干等几分钟，
        不如加载完就先在后台下载。
        """
        if self.browser_ready():
            logger.debug("浏览器已就绪，无需预安装")
            return
        await self._probe().ensure_browser_installed()

    async def search(self, image: ImageInput, *,
                     with_ocr: bool = False) -> LensSearchResult:
        """搜索一张图。浏览器没起来会自动启动。"""
        searcher = await self._ensure_searcher()
        try:
            return await searcher.search(image, with_ocr=with_ocr)
        finally:
            self._last_used = time.monotonic()

    async def ocr(self, image: ImageInput) -> list[str]:
        """只做 OCR。纯 HTTP，不需要浏览器。"""
        searcher = self._searcher or GoogleLensSearcher(self._config)
        return await searcher.ocr(image)

    async def _ensure_searcher(self) -> GoogleLensSearcher:
        async with self._lock:
            if self._searcher is None:
                logger.info("启动 Google Lens 浏览器会话")
                self._searcher = GoogleLensSearcher(self._config)
                await self._searcher.start()
                self._start_idle_watch()
            self._last_used = time.monotonic()
            return self._searcher

    def _start_idle_watch(self) -> None:
        if self._idle_close_seconds <= 0 or self._idle_task is not None:
            return
        self._idle_task = asyncio.create_task(self._idle_watch())

    async def _idle_watch(self) -> None:
        """空闲超时就关掉浏览器，下次用到会重新启动。"""
        try:
            while True:
                await asyncio.sleep(min(60, max(10, self._idle_close_seconds // 4)))
                if self._searcher is None:
                    return
                idle = time.monotonic() - self._last_used
                if idle >= self._idle_close_seconds:
                    logger.info("浏览器空闲 %.0f 秒，关闭以释放内存", idle)
                    await self.close(keep_watch=False)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("空闲检查任务异常退出: %s", exc)

    async def close(self, keep_watch: bool = False) -> None:
        """关闭浏览器。``keep_watch=False`` 时同时结束空闲检查任务。"""
        if self._closing:
            return
        self._closing = True
        try:
            if not keep_watch and self._idle_task is not None:
                task = self._idle_task
                self._idle_task = None
                if task is not asyncio.current_task():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            async with self._lock:
                searcher = self._searcher
                self._searcher = None
            if searcher is not None:
                await searcher.close()
        finally:
            self._closing = False
