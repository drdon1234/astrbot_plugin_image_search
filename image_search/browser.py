"""浏览器会话封装（CDP 附加真实 Chrome）。

为什么必须用浏览器：Google 自 2025-01-15 起要求 Search 页面执行 JavaScript，
``www.google.com/search`` 对无 JS 客户端只返回一个约 90KB 的引导脚本壳，
里面没有任何结果数据。Lens 的结果页（udm=26 / udm=48）同样如此。

为什么不用 Playwright 自己启动浏览器：它会带自动化标记，botguard 判定为
机器人后把 ``/search`` 转到 ``/sorry/index``。实测同一个机房 IP 下，
Playwright launch 必被拦，而普通启动 Chrome + CDP 附加可以正常拿到结果。
详见 :mod:`image_search.chrome`。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import pathlib
import time
import urllib.parse as up
from typing import Any

from .chrome import (
    ChromeProcess,
    browser_missing_message,
    find_chrome,
    locate_chrome,
    normalize_user_agent,
    profile_for,
    read_cached_user_agent,
    write_cached_user_agent,
)
from .config import SOCS_COOKIE, SearchConfig
from .exceptions import (
    BrowserNotAvailableError,
    FetchError,
    ParseError,
    RateLimitedError,
    UploadError,
)
from .installer import (
    BrowserInstaller,
    InstallState,
    default_browsers_dir,
    find_default_chromium,
)
from .logger import (
    exception_for_log,
    logger,
    path_for_log,
    quiet_http_logs,
    url_for_log,
)
from .parser import candidate_limit
from .uploader import to_ai_mode_url, to_exact_matches_url


@dataclasses.dataclass(slots=True)
class LensPageResult:
    """一次上传之后从各标签页抓到的原始产物。

    ``*_payload`` 是页面里提取脚本的返回值，还没解析；解析交给
    :mod:`image_search.parser`。哪一栏没抓就保持 ``None``。
    """

    lens_url: str
    exact_url: str = ""
    exact_payload: Any = None
    ai_url: str = ""
    ai_payload: Any = None
    exact_error: Exception | None = None
    ai_error: Exception | None = None

_CAPTCHA_HINT = (
    "Google 弹出了人机验证（/sorry/index）。\n"
    "这件事本身带随机性，GoogleLensSearcher.search() 会自动重试 "
    "（max_retries，默认 2 次）；这里是重试后仍未通过。\n"
    "排查顺序：\n"
    "  1. use_cdp 必须为 True。Playwright 自己启动浏览器会带自动化标记，必被拦；\n"
    "  2. 生效的 UA 里不能出现 HeadlessChrome，且版本号要和浏览器真实版本一致\n"
    "     （跑 python tools/diagnose.py 会打印实际 UA）；\n"
    "  3. 降低请求频率，并复用同一个 GoogleLensSearcher（cookie 和浏览器都能复用）；\n"
    "  4. 换出口 IP / 代理节点，机房 IP 的失败率明显更高；\n"
    "  5. 用 headless=False 手动过一次验证，豁免 cookie 会存进 profile。"
)
def playwright_version_mismatch() -> tuple[str, str] | None:
    """检查「进程里已加载的 playwright 客户端」和「磁盘上的版本」是否一致。

    返回 ``(进程内版本, 磁盘版本)``，一致或无法判断时返回 ``None``。

    为什么要查这个：AstrBot 在自己的进程里用 pip 安装插件依赖。如果某次安装
    升级了 playwright，磁盘上的客户端和 driver 都换成新版，但进程里早先
    ``import`` 的旧客户端仍在 ``sys.modules`` 中 —— **重载插件也不会替换它**。

    旧客户端去驱动新 driver，协议对不上。实测 1.49 客户端 + 1.62 driver 会在
    初始化时抛 ``KeyError: 'selectors'``（1.5x 之后 driver 不再下发这个字段）。
    要命的是这个异常发生在 ``Connection.run()`` 的后台任务里，主流程拿不到它，
    只会**永久挂起**：Playwright 的超时由 driver 端实现，客户端收不到任何消息
    就永远不会超时，我们传的 ``timeout`` 完全无效。

    实际表现是用户只收到「正在搜索」，然后再也等不到结果；而且
    :class:`GoogleLensSearcher` 的锁被永久持有，后续每一次搜索都会卡在等锁上，
    整个功能瘫痪且不会自愈。所以这里宁可提前拦下来报错。
    """
    try:
        import importlib.metadata as metadata

        from playwright._repo_version import version as loaded
    except Exception:  # noqa: BLE001
        # 私有模块，不保证一直存在；探测不到就跳过检查
        return None
    try:
        on_disk = metadata.version("playwright")
    except Exception:  # noqa: BLE001
        return None
    if loaded == on_disk:
        return None
    return loaded, on_disk


_VERSION_MISMATCH_HINT = (
    "playwright 版本不一致：AstrBot 进程里加载的是 {loaded}，磁盘上已经是 {on_disk}。\n"
    "通常是安装或更新插件时 pip 升级了 playwright，而 AstrBot 还没重启 —— "
    "已经 import 的旧客户端不会被替换，重载插件也没用。\n"
    "旧客户端驱动新版 driver 会直接卡死（不报错、不超时），所以这里提前拦下。\n"
    "解决办法：重启 AstrBot 或重启容器。"
)


class BrowserSession:
    """持有一个常驻浏览器上下文，跨多次搜索复用。

    复用很重要：冷启动既慢，也更容易触发人机验证（cookie 全新、行为像脚本）。
    """

    def __init__(self, config: SearchConfig) -> None:
        self._config = config
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._chrome: ChromeProcess | None = None
        self._installer: BrowserInstaller | None = None
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._startup_cleanup_tasks: set[asyncio.Task[None]] = set()

    @property
    def context(self) -> Any:
        if self._context is None:
            raise BrowserNotAvailableError("浏览器尚未启动，请先 await start()")
        return self._context

    @property
    def executable(self) -> str | None:
        """实际使用的浏览器可执行文件，用于诊断。"""
        return self._chrome.executable if self._chrome else None

    @property
    def user_agent(self) -> str | None:
        """实际生效的 UA，用于诊断。"""
        if not self._chrome:
            return None
        return self._chrome.user_agent or self._chrome.browser_user_agent

    # -- 启动 / 关闭 --------------------------------------------------------
    async def start(self) -> None:
        async with self._lock:
            await self._wait_for_cleanup()
            if await self._is_healthy():
                return
            if any((self._playwright, self._browser, self._context, self._chrome)):
                logger.warning("浏览器会话已失效，清理后重新启动")
                await asyncio.shield(self._begin_cleanup())
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover
                raise BrowserNotAvailableError(
                    "未安装 playwright，请执行 pip install playwright") from exc

            # 必须在 start() 之前拦：一旦让旧客户端去连新 driver 就会挂死
            mismatch = playwright_version_mismatch()
            if mismatch:
                loaded, on_disk = mismatch
                raise BrowserNotAvailableError(
                    _VERSION_MISMATCH_HINT.format(loaded=loaded, on_disk=on_disk))

            try:
                await self._start_playwright(async_playwright)
                if self._config.use_cdp:
                    await self._start_cdp()
                else:
                    await self._start_playwright_launch()
                await self._prepare_context()
            except asyncio.CancelledError:
                self._begin_cleanup()
                raise
            except Exception:
                await asyncio.shield(self._begin_cleanup())
                raise

    async def _start_playwright(self, factory: Any) -> None:
        """让 Playwright driver 的启动在取消时也能被取得并关闭。"""
        start_task = asyncio.create_task(factory().start())
        try:
            self._playwright = await asyncio.shield(start_task)
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(
                self._finish_cancelled_playwright_start(start_task))
            self._track_startup_cleanup(cleanup, "Playwright 启动")
            raise

    async def _is_healthy(self) -> bool:
        """同时检查 Python 连接状态和底层 Chrome 进程是否仍然存活。"""
        if self._context is None:
            return False
        if self._chrome is not None and not self._chrome.running:
            return False
        if self._browser is not None:
            try:
                if not self._browser.is_connected():
                    return False
            except Exception:  # noqa: BLE001
                return False
        try:
            probe = self._context.cookies(["https://www.google.com"])
            await asyncio.wait_for(probe, timeout=3)
        except Exception:  # noqa: BLE001
            return False
        return True

    def _begin_cleanup(self) -> asyncio.Task[None]:
        """复用正在运行的清理任务，并持有它直到真正结束。"""
        cleanup = self._cleanup_task
        if cleanup is not None and not cleanup.done():
            return cleanup
        cleanup = asyncio.create_task(self._teardown())
        self._cleanup_task = cleanup
        cleanup.add_done_callback(self._cleanup_finished)
        return cleanup

    def _cleanup_finished(self, cleanup: asyncio.Task[None]) -> None:
        if self._cleanup_task is cleanup:
            self._cleanup_task = None
        if cleanup.cancelled():
            return
        error = cleanup.exception()
        if error is not None:
            logger.debug("浏览器后台清理失败: %s", exception_for_log(error))

    async def _wait_for_cleanup(self) -> None:
        while True:
            tasks = [
                task for task in self._startup_cleanup_tasks
                if not task.done()
            ]
            cleanup = self._cleanup_task
            if cleanup is not None and not cleanup.done():
                tasks.append(cleanup)
            if not tasks:
                return
            await asyncio.gather(
                *(asyncio.shield(task) for task in tasks),
                return_exceptions=True,
            )

    def _track_startup_cleanup(self, task: asyncio.Task[None], label: str) -> None:
        """强引用不可取消的启动收尾，并统一取走异常。"""
        self._startup_cleanup_tasks.add(task)
        task.add_done_callback(
            lambda done: self._startup_cleanup_finished(done, label))

    def _startup_cleanup_finished(
        self,
        task: asyncio.Task[None],
        label: str,
    ) -> None:
        self._startup_cleanup_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning("%s取消后的资源回收失败: %s",
                           label, exception_for_log(error))

    @staticmethod
    async def _finish_cancelled_playwright_start(
        start_task: asyncio.Task[Any],
    ) -> None:
        """取得迟到创建的 Playwright 实例并立即停止它。"""
        try:
            playwright = await start_task
        except Exception:
            return
        await playwright.stop()

    @staticmethod
    async def _finish_chrome_start(
        start_task: asyncio.Task[None],
        chrome: ChromeProcess,
    ) -> None:
        """等待不可取消的线程启动结束，再回收它可能创建的进程。"""
        try:
            await start_task
        except Exception:
            pass
        await asyncio.to_thread(chrome.stop)

    async def _start_cdp(self) -> None:
        """普通方式启动浏览器，再通过 CDP 附加。默认路径。"""
        cfg = self._config
        executable = await self._resolve_executable()
        # profile 按浏览器隔离：不同版本的浏览器共用 profile 会起不来
        profile = profile_for(cfg.resolved_user_data_dir(), executable)
        logger.debug("使用浏览器 %s（profile=%s）",
                     path_for_log(executable), path_for_log(profile))

        self._chrome = await self._launch_chrome(
            executable, profile, read_cached_user_agent(profile, executable))

        # 无头模式的 UA 会带 HeadlessChrome，这一条就足以被 Google 拦下。
        # 首次启动时探测真实 UA，改掉 Headless 标记后重启一次，结果缓存起来。
        if self._chrome.user_agent is None:
            fixed = normalize_user_agent(self._chrome.browser_user_agent)
            if fixed:
                logger.debug("UA 含 HeadlessChrome，改写后重启: %s", fixed)
                browser_version = self._chrome.browser_version
                await asyncio.to_thread(self._chrome.stop)
                write_cached_user_agent(
                    profile, executable, fixed, browser_version)
                self._chrome = await self._launch_chrome(executable, profile, fixed)

        self._browser = await self._playwright.chromium.connect_over_cdp(
            self._chrome.cdp_url, timeout=cfg.timeout_ms)
        self._context = (self._browser.contexts[0] if self._browser.contexts
                         else await self._browser.new_context())

    async def _launch_chrome(self, executable: str, profile: pathlib.Path,
                             user_agent: str | None) -> ChromeProcess:
        cfg = self._config
        chrome = ChromeProcess(
            executable=executable,
            user_data_dir=profile,
            headless=cfg.headless,
            proxy=cfg.proxy,
            lang=f"{cfg.hl}-US" if cfg.hl == "en" else cfg.hl,
            window_size=cfg.window_size,
            user_agent=user_agent,
            no_sandbox=cfg.no_sandbox,
        )
        start_task = asyncio.create_task(asyncio.to_thread(chrome.start))
        try:
            await asyncio.shield(start_task)
        except asyncio.CancelledError:
            # to_thread 本身无法被取消；等 start() 到达终态后再收进程，
            # 否则可能在 stop() 返回之后才姗姗来迟地 spawn 出孤儿 Chrome。
            cleanup = asyncio.create_task(
                self._finish_chrome_start(start_task, chrome))
            self._track_startup_cleanup(cleanup, "Chrome 启动")
            raise
        except Exception:
            cleanup = asyncio.create_task(
                self._finish_chrome_start(start_task, chrome))
            self._track_startup_cleanup(cleanup, "Chrome 启动")
            await asyncio.shield(cleanup)
            raise
        return chrome

    def _bundled_chromium(self) -> str | None:
        """Playwright 默认位置的 Chromium 路径（可能并不存在）。"""
        try:
            path = self._playwright.chromium.executable_path
            if pathlib.Path(path).is_file():
                return path
        except Exception:  # noqa: BLE001
            pass
        return find_default_chromium()

    async def _resolve_executable(self) -> str:
        """定位浏览器；缺失且开了自动安装就先装再找。"""
        cfg = self._config
        bundled = self._bundled_chromium()
        explicit = None if cfg.prefer_bundled_chromium else cfg.chrome_path
        if cfg.prefer_bundled_chromium and bundled:
            explicit = bundled
        install_dir = self.browsers_dir

        path, checked = locate_chrome(
            explicit,
            bundled,
            install_dir,
            allow_unmanaged_install=not cfg.auto_install_browser,
        )
        if path:
            return path

        if not cfg.auto_install_browser:
            raise BrowserNotAvailableError(
                browser_missing_message(checked, install_dir,
                                        auto_install_enabled=False))

        logger.info("没找到浏览器，开始自动安装")
        path = await self.installer.ensure()
        if path:
            return path
        raise BrowserNotAvailableError(
            browser_missing_message(checked, install_dir,
                                    auto_install_enabled=True)
            + f"\n\n自动安装状态：{self.installer.status_text()}")

    # -- 浏览器自动安装 -----------------------------------------------------
    @property
    def browsers_dir(self) -> pathlib.Path:
        """浏览器安装目录。放插件数据目录下，容器重建也不会丢。"""
        if self._config.browser_install_dir:
            return pathlib.Path(self._config.browser_install_dir)
        return default_browsers_dir(self._config.resolved_user_data_dir().parent)

    @property
    def installer(self) -> BrowserInstaller:
        if self._installer is None:
            self._installer = BrowserInstaller(
                self.browsers_dir,
                with_deps=self._config.install_system_deps,
                timeout=self._config.install_timeout_seconds,
            )
        return self._installer

    def browser_ready(self) -> bool:
        """当前是否已经有可用的浏览器（不启动 Playwright，纯文件检查）。"""
        bundled = find_default_chromium()
        explicit = (bundled if self._config.prefer_bundled_chromium and bundled
                    else self._config.chrome_path)
        path, _ = locate_chrome(
            explicit,
            bundled,
            self.browsers_dir,
            allow_unmanaged_install=not self._config.auto_install_browser,
        )
        return path is not None

    async def ensure_browser_installed(self) -> str | None:
        """提前把浏览器装好。适合插件加载后在后台调用，避免首次搜索干等。"""
        if self.browser_ready():
            return "already"
        if not self._config.auto_install_browser:
            self.installer.state = InstallState.SKIPPED
            return None
        return await self.installer.ensure()

    async def _start_playwright_launch(self) -> None:
        """由 Playwright 直接启动（会被 Google 识别为自动化，仅作后备）。"""
        cfg = self._config
        kwargs: dict[str, Any] = {
            "user_data_dir": str(cfg.resolved_user_data_dir()),
            "headless": cfg.headless,
            "user_agent": cfg.user_agent,
            "viewport": {"width": cfg.window_size[0], "height": cfg.window_size[1]},
            "args": [f"--lang={cfg.hl}-US",
                     "--disable-blink-features=AutomationControlled"],
        }
        if cfg.proxy:
            kwargs["proxy"] = {"server": cfg.proxy}
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                channel="chrome", **kwargs)
        except Exception:  # noqa: BLE001
            self._context = await self._playwright.chromium.launch_persistent_context(
                **kwargs)

    async def _prepare_context(self) -> None:
        try:
            await self._context.add_cookies([{
                "name": "SOCS", "value": SOCS_COOKIE,
                "domain": ".google.com", "path": "/",
            }])
        except Exception:  # noqa: BLE001
            pass
        self._context.set_default_timeout(self._config.timeout_ms)
        if self._config.warmup:
            await self._warmup()

    async def reset_session(self) -> None:
        """清掉 cookie 换一个干净会话，然后重新预热。

        撞过一次人机验证后，这个 profile 的 cookie 就被 Google 标记了，
        原样重试会一直失败（实测复用被污染的 profile 是 0/4，
        每次用全新 profile 则是 4/4）。所以重试前必须先把会话清干净。
        """
        if self._context is None:
            return
        try:
            await self._context.clear_cookies()
        except Exception as exc:  # noqa: BLE001
            logger.debug("清 cookie 失败: %s", exception_for_log(exc))
            return
        await self._prepare_context()

    async def _warmup(self) -> None:
        """先访问一次 Google 首页，拿到正常的 NID 等 cookie。

        全新 profile 的第一个请求就是 Lens 结果页显得很反常。这一步很便宜
        （整个浏览器生命周期只做一次），失败也不影响后续流程。
        """
        page = await self._context.new_page()
        try:
            await page.goto(f"https://www.google.com/?hl={self._config.hl}",
                            wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(1500)
            for selector in ("#L2AGLb", 'button:has-text("Accept all")'):
                element = await page.query_selector(selector)
                if element and await element.is_visible():
                    await element.click()
                    await page.wait_for_timeout(1000)
                    break
            if "/sorry/" in page.url:
                logger.debug("预热时就撞上了人机验证，出口 IP 可能信誉不佳")
        except Exception as exc:  # noqa: BLE001
            logger.debug("预热失败，忽略: %s", exception_for_log(exc))
        finally:
            await page.close()

    async def close(self) -> None:
        async with self._lock:
            await self._wait_for_cleanup()
            await asyncio.shield(self._begin_cleanup())

    async def _teardown(self) -> None:
        context, self._context = self._context, None
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        chrome, self._chrome = self._chrome, None

        if chrome is not None:
            try:
                await asyncio.to_thread(chrome.stop)
            except Exception:  # noqa: BLE001
                pass
        if context is not None and browser is None:
            # launch_persistent_context 拿到的是 context，关它即可
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:  # noqa: BLE001
                pass

    async def __aenter__(self) -> BrowserSession:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- 页面操作 -----------------------------------------------------------
    async def _new_page(self) -> Any:
        await self.start()
        # CDP 附加时 Chrome 已经有一个 about:blank 标签，复用它可以少开一个页面
        pages = [p for p in self._context.pages if p.url in ("about:blank", "")]
        if pages:
            return pages[0], False
        return await self._context.new_page(), True

    async def _dismiss_consent(self, page: Any) -> None:
        """点掉 cookie 同意弹窗，挡住的话后面什么都点不到。"""
        for selector in ("#L2AGLb", 'button:has-text("Accept all")',
                         'button:has-text("Reject all")'):
            try:
                element = await page.query_selector(selector)
                if element and await element.is_visible():
                    await element.click()
                    await page.wait_for_timeout(1200)
                    return
            except Exception:  # noqa: BLE001
                continue

    async def upload_and_extract(self, image: bytes, filename: str, mime: str,
                                 exact_script: str | None = None,
                                 ai_script: str | None = None,
                                 debug_name: str | None = None,
                                 ) -> LensPageResult:
        """在浏览器里上传图片，然后按需抓「完全匹配」和「AI 模式」两个标签页。

        为什么上传要在浏览器里做：``vsrid`` 会话绑定在上传方的身份上，
        换别的客户端上传、再让浏览器打开结果页，页面会显示
        "Expired visual search"。而 Playwright 1.5x 之后
        ``context.request`` 已经不再和 CDP 附加的上下文共享会话
        （1.49 时还共享），所以只剩「走真实上传界面」这条稳的路 ——
        它本来也最贴近真实用户行为。

        两种模式共用同一次上传：拿到 ``vsrid`` 之后只要改 ``udm`` 就能切标签，
        所以开两个模式的代价只是多渲染一个页面，不用重新上传。

        Args:
            exact_script: 完全匹配页上执行的提取脚本；``None`` 表示不抓这一栏。
            ai_script: AI 模式页上执行的提取脚本；``None`` 表示不抓。
        """
        cfg = self._config
        page, opened = await self._new_page()
        try:
            try:
                await page.goto(f"https://www.google.com/?olud&hl={cfg.hl}",
                                wait_until="domcontentloaded",
                                timeout=cfg.timeout_ms)
            except Exception as exc:  # noqa: BLE001
                raise FetchError(
                    f"打开 Lens 上传页失败: {type(exc).__name__}") from exc
            self._assert_not_blocked(page.url)
            await page.wait_for_timeout(2000)
            await self._dismiss_consent(page)

            lens_url = await self._submit_image(page, image, filename, mime)
            logger.debug("已取得上传结果页 %s", url_for_log(lens_url))
            outcome = LensPageResult(lens_url=lens_url)

            if exact_script is not None:
                outcome.exact_url = to_exact_matches_url(
                    lens_url, cfg.hl, cfg.safe_search)
                logger.debug("准备采集完全匹配页")
                try:
                    outcome.exact_payload = await self._collect_exact(
                        page, outcome.exact_url, exact_script, debug_name)
                except RateLimitedError:
                    raise
                except (FetchError, ParseError) as exc:
                    if ai_script is None:
                        raise
                    outcome.exact_error = exc
                    logger.warning("完全匹配抓取失败，继续尝试 AI 模式: %s",
                                   exception_for_log(exc))

            if ai_script is not None:
                outcome.ai_url = to_ai_mode_url(lens_url, cfg.hl,
                                                cfg.safe_search)
                logger.debug("准备采集 AI 模式页")
                try:
                    outcome.ai_payload = await self._collect_ai(
                        page, outcome.ai_url, ai_script, debug_name)
                except RateLimitedError:
                    raise
                except (FetchError, ParseError) as exc:
                    if exact_script is None:
                        raise
                    outcome.ai_error = exc
                    logger.warning("AI 模式抓取失败，保留完全匹配结果: %s",
                                   exception_for_log(exc))
            return outcome
        finally:
            if opened:
                await page.close()
            else:
                try:
                    await page.goto("about:blank")
                except Exception:  # noqa: BLE001
                    pass

    async def _collect_exact(self, page: Any, url: str, script: str,
                             debug_name: str | None) -> dict[str, Any]:
        """打开完全匹配页，按候选数量稳定条件完成采集。"""
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=self._config.timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise FetchError(
                f"打开完全匹配页失败: {type(exc).__name__}") from exc
        self._assert_not_blocked(page.url)
        payload = await self._settle(page, script)
        if (not isinstance(payload, dict)
                or not isinstance(payload.get("items"), list)):
            raise ParseError("完全匹配页面脚本返回了无效数据，页面结构可能已变化")
        if self._config.debug_dir and debug_name:
            await self._dump(page, debug_name)
        return payload

    async def _collect_ai(self, page: Any, url: str, script: str,
                          debug_name: str | None) -> dict[str, Any]:
        """打开 AI 模式页，先等正文出现，再单独计算回答收敛时间。

        AI 的回答是流式输出的，打开页面时才刚开始写。这里等到「已经开始生成」
        且「字数连续几轮不再增长」为止，实测 11~12 秒收敛。
        """
        cfg = self._config
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=cfg.timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise FetchError(
                f"打开 AI 模式页失败: {type(exc).__name__}") from exc
        self._assert_not_blocked(page.url)

        wait_seconds = max(0.1, cfg.ai_wait_ms / 1000)
        start_deadline = time.monotonic() + wait_seconds
        convergence_deadline: float | None = None
        payload: dict[str, Any] | None = None
        last, stable, evaluate_failures = -1, 0, 0
        while True:
            deadline = convergence_deadline or start_deadline
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await page.wait_for_timeout(min(1200, max(100, int(remaining * 1000))))
            try:
                current = await page.evaluate(script)
            except Exception as exc:  # noqa: BLE001
                evaluate_failures += 1
                if evaluate_failures < 3:
                    logger.debug("AI 模式提取脚本暂时失败: %s",
                                 type(exc).__name__)
                    continue
                raise ParseError(
                    f"AI 模式提取脚本连续失败: {type(exc).__name__}") from exc
            payload = self._validate_ai_payload(current)
            evaluate_failures = 0
            count = payload["charCount"]
            if not payload["started"] or count <= 0:
                continue
            if convergence_deadline is None:
                # 生成前的排队时间不应蚕食正文生成时间。
                convergence_deadline = time.monotonic() + wait_seconds
                last, stable = count, 0
                continue
            if count == last:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable, last = 0, count
        if (payload is None or not payload["started"]
                or payload["charCount"] <= 0):
            raise ParseError(
                f"AI 模式在 {cfg.ai_wait_ms} ms 内没有生成可用正文")
        if convergence_deadline and time.monotonic() >= convergence_deadline:
            logger.debug("AI 回答在 %d ms 内没有收敛，用当前内容",
                         cfg.ai_wait_ms)
        if cfg.debug_dir and debug_name:
            await self._dump(page, f"{debug_name}_ai")
        return payload

    @staticmethod
    def _validate_ai_payload(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ParseError(
                f"AI 页面脚本返回了意外类型: {type(payload).__name__}")
        started = payload.get("started")
        html = payload.get("html")
        count = payload.get("charCount")
        if (type(started) is not bool or not isinstance(html, str)
                or type(count) is not int or count < 0):
            raise ParseError("AI 页面脚本返回的数据结构无效，页面结构可能已变化")
        if count > 0 and not html.strip():
            raise ParseError("AI 页面报告已有正文，但没有返回正文 HTML")
        return payload

    async def _submit_image(self, page: Any, image: bytes, filename: str,
                            mime: str) -> str:
        """把图片塞进 Lens 的上传输入框，等 Google 跳到结果页。

        跳转后必须再等页面稳定一会儿才能读地址：Google 会**逐步补全**
        查询参数（``gsessionid`` / ``lsessionid`` 等），一看到 ``vsrid``
        就立刻拿走地址的话，拿到的是不完整的地址，
        后面按它改写出的完全匹配页会一条结果都没有。
        """
        payload = {"name": filename, "mimeType": mime, "buffer": image}
        inputs = await page.query_selector_all('input[type="file"]')
        if not inputs:
            await page.wait_for_timeout(2000)
            inputs = await page.query_selector_all('input[type="file"]')
        if not inputs:
            raise UploadError("Lens 上传页里找不到文件输入框，页面结构可能变了")

        # 页面上有多个 file input，只有其中一个是 Lens 的。倒序试 ——
        # 实测 Lens 那个通常排在最后。探测阶段等待时间给短一点，避免白等。
        for index, element in reversed(list(enumerate(inputs))):
            try:
                await element.set_input_files(payload)
            except Exception as exc:  # noqa: BLE001
                logger.debug("file input[%d] 不接受文件: %s", index,
                             exception_for_log(exc))
                continue
            navigated = False
            for _ in range(12):
                await page.wait_for_timeout(1000)
                self._assert_not_blocked(page.url)
                if "vsrid" in page.url:
                    navigated = True
                    break
            if not navigated:
                logger.debug("file input[%d] 塞进去了但没跳转", index)
                continue

            # Google 会逐步补齐结果页查询参数。按 URL 连续稳定来判断完成，
            # 避免无论快慢都固定再睡四秒。
            deadline = time.monotonic() + min(
                12.0, max(3.0, self._config.timeout_ms / 1000))
            last_url, stable = page.url, 0
            while time.monotonic() < deadline:
                await page.wait_for_timeout(600)
                self._assert_not_blocked(page.url)
                if page.url == last_url:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    last_url, stable = page.url, 0
            return page.url
        raise UploadError(
            "上传后没有跳转到结果页。可能是图片被拒绝，或 Lens 页面结构变了")

    async def _settle(self, page: Any, script: str) -> Any:
        """等候选数量稳定或达到超采样目标，并按需触发有限次懒加载。"""
        try:
            await page.wait_for_load_state(
                "networkidle", timeout=min(10_000, self._config.timeout_ms))
        except Exception:  # noqa: BLE001
            pass
        deadline = time.monotonic() + max(2.0, self._config.settle_ms / 1000)
        target = candidate_limit(self._config.max_results)
        payload: Any = None
        last_count, stable, scrolls = -1, 0, 0

        while True:
            self._assert_not_blocked(page.url)
            try:
                payload = await page.evaluate(script)
            except Exception as exc:  # noqa: BLE001
                raise ParseError(
                    f"结果页提取脚本执行失败: {type(exc).__name__}") from exc
            if not isinstance(payload, dict) or not isinstance(
                    payload.get("items"), list):
                return payload

            count = len(payload["items"])
            if target == 0 or count >= target:
                return payload
            if count == last_count:
                stable += 1
                if count > 0 and stable >= 2:
                    return payload
            else:
                last_count, stable = count, 0

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return payload
            if scrolls < 5:
                await page.mouse.wheel(0, 2200)
                scrolls += 1
            await page.wait_for_timeout(min(700, max(100, int(remaining * 1000))))

    async def render_and_extract(self, url: str, script: str,
                                 debug_name: str | None = None) -> Any:
        """打开 url，等页面稳定后在页面上下文里执行 ``script`` 并返回结果。

        在页面里取数据比先拿 HTML 再离线解析更准：``innerText`` 只包含真正
        可见的文字，不会把隐藏节点算进来。

        Raises:
            RateLimitedError: 命中 ``/sorry/index`` 人机验证。
            FetchError: 导航失败。
        """
        cfg = self._config
        page, opened = await self._new_page()
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=cfg.timeout_ms)
            except Exception as exc:  # noqa: BLE001
                raise FetchError(f"打开结果页失败: {type(exc).__name__}") from exc

            self._assert_not_blocked(page.url)
            data = await self._settle(page, script)
            if cfg.debug_dir and debug_name:
                await self._dump(page, debug_name)
            return data
        finally:
            if opened:
                await page.close()
            else:
                try:
                    await page.goto("about:blank")
                except Exception:  # noqa: BLE001
                    pass

    async def render(self, url: str, debug_name: str | None = None) -> str:
        """打开 url 并返回渲染后的 HTML。"""
        cfg = self._config
        page, opened = await self._new_page()
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=cfg.timeout_ms)
            except Exception as exc:  # noqa: BLE001
                raise FetchError(f"打开页面失败: {type(exc).__name__}") from exc
            self._assert_not_blocked(page.url)
            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(cfg.settle_ms)
            self._assert_not_blocked(page.url)
            if cfg.debug_dir and debug_name:
                await self._dump(page, debug_name)
            return await page.content()
        finally:
            if opened:
                await page.close()

    async def resolve_redirects(self, urls: list[str], referer: str = "",
                               concurrency: int = 6) -> list[str | None]:
        """把 ``/goto?url=...`` 之类的跳板地址批量还原成真实地址。

        Lens 结果页里没有明文的目标地址，只有不透明编码的跳板链接，
        必须请求一次读 302 的 ``Location``。

        这里用 httpx 而不是浏览器上下文的 ``context.request``：CDP 附加的
        上下文**不会**把浏览器的 ``--proxy-server`` 转给 ``context.request``，
        那些请求是 Playwright 进程自己直连发出去的。容器里没有系统代理，
        于是全部超时，一条链接都还原不出来 —— 而 :meth:`_resolve` 会把没有
        地址的条目丢掉，最终表现是「页面明明抽到 20 张卡片，却返回 0 条结果」，
        且每次白等满一个超时。实测同一环境下 ``context.request`` 0/5、耗时
        60s，换成 httpx 带上 ``config.proxy`` 是 5/5、耗时 0.6s。

        跳板解码不依赖会话，实测不带 cookie 也能正常还原。
        """
        if not urls:
            return []
        import httpx

        headers = {"User-Agent": self.user_agent or self._config.user_agent}
        if referer:
            headers["Referer"] = referer
        # 跳板只是一次解码重定向，实测 0.6s 就够；给太长的超时只会让
        # 个别卡住的链接拖慢整批
        timeout = min(20.0, self._config.timeout_ms / 1000)
        semaphore = asyncio.Semaphore(max(1, int(concurrency)))

        client_options: dict[str, Any] = {
            "follow_redirects": False,
            "timeout": timeout,
            "headers": headers,
            "proxy": self._config.proxy,
        }

        with quiet_http_logs():
            async with httpx.AsyncClient(**client_options) as client:
                async def follow(url: str) -> str | None:
                    current = url
                    for _ in range(3):
                        resp = await client.get(current)
                        if resp.status_code in (403, 429):
                            raise RateLimitedError(_CAPTCHA_HINT)
                        location = resp.headers.get("location", "")
                        if not location:
                            logger.debug(
                                "跳板未返回重定向（status=%s）",
                                resp.status_code)
                            return None

                        target = up.urljoin(str(resp.url), location)
                        if "/sorry/" in target:
                            raise RateLimitedError(_CAPTCHA_HINT)
                        if not target.lower().startswith(("http://", "https://")):
                            logger.debug("跳板返回了非 HTTP(S) 地址")
                            return None
                        target_parts = up.urlsplit(target)
                        current_host = (up.urlsplit(current).hostname or "").lower()
                        if (target_parts.hostname or "").lower() != current_host:
                            return target
                        current = target
                    logger.debug("跳板重定向次数超过上限")
                    return None

                async def resolve(url: str) -> str | None:
                    async with semaphore:
                        try:
                            # 把候选的完整跳转链包进同一个截止时间，避免逐跳
                            # 叠加阶段超时。
                            return await asyncio.wait_for(
                                follow(url), timeout=timeout)
                        except RateLimitedError:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("还原跳板失败: %s",
                                         exception_for_log(exc))
                            return None

                resolved = await asyncio.gather(
                    *(resolve(u) for u in urls), return_exceptions=True)
                for item in resolved:
                    if isinstance(item, RateLimitedError):
                        raise item
                return [item if isinstance(item, str) else None
                        for item in resolved]

    @staticmethod
    def _assert_not_blocked(url: str) -> None:
        if "/sorry/" in url:
            raise RateLimitedError(_CAPTCHA_HINT)

    async def _dump(self, page: Any, name: str) -> None:
        directory = pathlib.Path(self._config.debug_dir)
        directory.mkdir(parents=True, exist_ok=True)
        try:
            (directory / f"{name}.html").write_text(
                await page.content(), encoding="utf-8", errors="replace")
            # 只截可视区域：Lens 结果页整页截图能到几十 MB，没必要
            await page.screenshot(path=str(directory / f"{name}.png"))
        except Exception:  # noqa: BLE001
            pass
