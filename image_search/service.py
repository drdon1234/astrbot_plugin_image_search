"""长驻服务封装：请求准入、懒启动和空闲自动关闭浏览器。

机器人插件的使用特点是「偶发调用、间隔很长」。浏览器常驻能省掉首次 UA 探测
和预热（单次从 23 秒降到 12 秒），但一直挂着又白占几百 MB 内存。所以这里做成
用到才启动、空闲一段时间后自动关闭，下次再用又会自动拉起来。
"""

from __future__ import annotations

import asyncio
import time

from .browser import BrowserSession
from .config import SearchConfig
from .exceptions import SearchBusyError, SearchTimeoutError
from .installer import InstallState
from .loader import ImageInput
from .logger import exception_for_log, logger
from .models import LensSearchResult
from .searcher import GoogleLensSearcher


class LensSearchService:
    """把 :class:`GoogleLensSearcher` 包成适合常驻进程用的服务。

    Args:
        config: 搜索配置。
        idle_close_seconds: 空闲多少秒后关掉浏览器；``0`` 表示常驻不关。
        max_pending_searches: 活动搜索之外最多允许等待的请求数；``0`` 表示
            有搜索正在执行时直接拒绝新请求。
    """

    def __init__(self, config: SearchConfig,
                 idle_close_seconds: int = 1800,
                 max_pending_searches: int = 1) -> None:
        self._config = config
        self._idle_close_seconds = max(0, idle_close_seconds)
        self._max_pending_searches = max(0, max_pending_searches)
        self._searcher: GoogleLensSearcher | None = None
        self._probe_session: BrowserSession | None = None
        self._execution_lock = asyncio.Lock()
        self._admission_lock = asyncio.Lock()
        self._admitted_searches = 0
        self._active_task: asyncio.Task[object] | None = None
        self._last_used = 0.0
        self._idle_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[object]] = set()
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def config(self) -> SearchConfig:
        return self._config

    @property
    def running(self) -> bool:
        return self._searcher is not None

    @property
    def active(self) -> bool:
        """当前是否有请求持有搜索执行权。"""
        return self._active_task is not None

    @property
    def queued(self) -> int:
        """当前等待执行权的请求数。"""
        return max(0, self._admitted_searches - (1 if self.active else 0))

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
        if self._closed or self._closing:
            return
        if self.browser_ready():
            logger.debug("浏览器已就绪，无需预安装")
            return
        await self._probe().ensure_browser_installed()

    async def search(self, image: ImageInput, *, with_ocr: bool = False,
                     timeout_seconds: int = 0) -> LensSearchResult:
        """搜索一张图，并统一管理排队、执行超时和浏览器会话。

        总超时只从请求取得执行权后开始计算。超时请求仍持有执行权时会丢弃
        当前会话；其它排队或刚进入的请求无权关闭共享浏览器。

        Raises:
            SearchBusyError: 活动槽与有限等待队列都已占满。
            SearchTimeoutError: 取得执行权后的搜索超过 ``timeout_seconds``。
        """
        await self._admit()
        try:
            async with self._execution_lock:
                if self._closed:
                    raise SearchBusyError("搜图服务已关闭")
                if self._closing:
                    raise SearchBusyError("搜图服务正在关闭，请稍后再试")
                self._active_task = asyncio.current_task()
                try:
                    return await self._run_operation(
                        image,
                        with_ocr=with_ocr,
                        timeout_seconds=timeout_seconds,
                    )
                finally:
                    self._active_task = None
                    self._last_used = time.monotonic()
        finally:
            async with self._admission_lock:
                self._admitted_searches = max(0, self._admitted_searches - 1)

    async def _admit(self) -> None:
        """原子地占一个活动或等待名额。"""
        async with self._admission_lock:
            capacity = 1 + self._max_pending_searches
            if self._closed:
                raise SearchBusyError("搜图服务已关闭")
            if self._closing:
                raise SearchBusyError("搜图服务正在关闭，请稍后再试")
            if self._admitted_searches >= capacity:
                if self._max_pending_searches:
                    raise SearchBusyError(
                        "当前已有搜图任务在执行，等待队列也已满，请稍后再试")
                raise SearchBusyError("当前已有搜图任务在执行，请稍后再试")
            self._admitted_searches += 1

    async def _execute_search(self, image: ImageInput, *,
                              with_ocr: bool) -> LensSearchResult:
        searcher = await self._ensure_searcher()
        return await searcher.search(image, with_ocr=with_ocr)

    async def _run_operation(self, image: ImageInput, *, with_ocr: bool,
                             timeout_seconds: int) -> LensSearchResult:
        """以硬截止时间运行搜索，不无限等待底层响应取消。"""
        task = asyncio.create_task(
            self._execute_search(image, with_ocr=with_ocr))
        try:
            if timeout_seconds <= 0:
                return await task
            done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
            if task in done:
                return task.result()

            task.cancel()
            self._track_background_task(task, "搜索任务")
            logger.error(
                "搜索取得执行权后超过 %d 秒，重置当前浏览器会话",
                timeout_seconds,
            )
            await self._discard_searcher()
            raise SearchTimeoutError
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
                self._track_background_task(task, "搜索任务")
            await self._discard_searcher()
            raise

    def _track_background_task(self, task: asyncio.Task[object],
                               label: str) -> None:
        """强引用后台任务，完成后统一取走异常。"""
        self._background_tasks.add(task)
        task.add_done_callback(
            lambda done: self._consume_background_task(done, label))

    def _consume_background_task(self, task: asyncio.Task[object],
                                 label: str) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("后台%s结束时出错: %s",
                           label, exception_for_log(exc))

    async def ocr(self, image: ImageInput) -> list[str]:
        """只做 OCR；复用与搜索相同的准入和最终关闭语义。"""
        await self._admit()
        try:
            async with self._execution_lock:
                if self._closed:
                    raise SearchBusyError("搜图服务已关闭")
                if self._closing:
                    raise SearchBusyError("搜图服务正在关闭，请稍后再试")
                self._active_task = asyncio.current_task()
                try:
                    searcher = self._searcher or GoogleLensSearcher(self._config)
                    return await searcher.ocr(image)
                finally:
                    self._active_task = None
                    self._last_used = time.monotonic()
        finally:
            async with self._admission_lock:
                self._admitted_searches = max(0, self._admitted_searches - 1)

    async def _ensure_searcher(self) -> GoogleLensSearcher:
        if self._searcher is None:
            logger.info("启动 Google Lens 浏览器会话")
            searcher = GoogleLensSearcher(self._config)
            self._searcher = searcher
            try:
                await searcher.start()
            except asyncio.CancelledError:
                # 外层活动请求负责摘除并关闭，避免同一会话并发 close。
                raise
            except Exception:
                if self._searcher is searcher:
                    self._searcher = None
                await self._close_searcher(searcher)
                raise
            self._start_idle_watch()
        self._last_used = time.monotonic()
        return self._searcher

    def _start_idle_watch(self) -> None:
        if self._idle_task is not None and self._idle_task.done():
            self._idle_task = None
        if (self._closed or self._closing or self._idle_close_seconds <= 0
                or self._idle_task is not None):
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
                    if await self._close_if_still_idle():
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("空闲检查任务异常退出: %s",
                           exception_for_log(exc))
        finally:
            if self._idle_task is asyncio.current_task():
                self._idle_task = None

    async def _close_if_still_idle(self) -> bool:
        """拿到执行锁后复核空闲状态，绝不打断活动或已排队的搜索。"""
        async with self._execution_lock:
            async with self._admission_lock:
                if self._admitted_searches or self._closing:
                    return False
            if self._searcher is None:
                return True
            idle = time.monotonic() - self._last_used
            if idle < self._idle_close_seconds:
                return False
            logger.info("浏览器空闲 %.0f 秒，关闭以释放内存", idle)
            await self._discard_searcher()
            return True

    async def close(self, *, cancel_active: bool = False) -> None:
        """永久关闭服务；并发调用复用同一个关闭任务。

        ``close()`` 是插件卸载语义，不是临时回收浏览器。首次调用后永久停止
        接受新请求；调用方被取消或等待超时也不会取消底层清理。
        """
        async with self._admission_lock:
            self._closed = True
            task = self._close_task
            if task is None:
                self._closing = True
                task = asyncio.create_task(self._close_impl())
                self._close_task = task
            active = self._active_task

        if (cancel_active and active is not None
                and active is not asyncio.current_task()):
            active.cancel()
        await asyncio.shield(task)

    async def _close_impl(self) -> None:
        """执行一次最终关闭；由 :meth:`close` 持有并复用任务。"""
        try:
            if self._idle_task is not None:
                task = self._idle_task
                self._idle_task = None
                if task is not asyncio.current_task():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

            async with self._execution_lock:
                await self._discard_searcher()
        finally:
            async with self._admission_lock:
                self._closing = False

    async def _discard_searcher(self) -> None:
        """先摘除共享引用，再有界关闭对应浏览器会话。"""
        searcher = self._searcher
        self._searcher = None
        if searcher is not None:
            await self._close_searcher(searcher)

    async def _close_searcher(self, searcher: GoogleLensSearcher) -> None:
        task = asyncio.create_task(searcher.close())
        try:
            done, _ = await asyncio.wait({task}, timeout=20)
        except asyncio.CancelledError:
            self._track_background_task(task, "浏览器会话关闭任务")
            raise

        if task not in done:
            self._track_background_task(task, "浏览器会话关闭任务")
            logger.error("关闭浏览器会话超过 20 秒，转入后台继续清理")
            return
        try:
            task.result()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭浏览器会话失败: %s", exception_for_log(exc))
