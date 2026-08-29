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
import pathlib
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
    RateLimitedError,
    UploadError,
)
from .installer import (
    BrowserInstaller,
    InstallState,
    default_browsers_dir,
)
from .logger import logger, quiet_http_logs
from .uploader import to_exact_matches_url

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
            if self._context is not None:
                return
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

            self._playwright = await async_playwright().start()
            try:
                if self._config.use_cdp:
                    await self._start_cdp()
                else:
                    await self._start_playwright_launch()
            except Exception:
                await self._teardown()
                raise
            await self._prepare_context()

    async def _start_cdp(self) -> None:
        """普通方式启动浏览器，再通过 CDP 附加。默认路径。"""
        cfg = self._config
        executable = await self._resolve_executable()
        # profile 按浏览器隔离：不同版本的浏览器共用 profile 会起不来
        profile = profile_for(cfg.resolved_user_data_dir(), executable)
        logger.debug("使用浏览器: %s (profile=%s)", executable, profile)

        self._chrome = await self._launch_chrome(
            executable, profile, read_cached_user_agent(profile, executable))

        # 无头模式的 UA 会带 HeadlessChrome，这一条就足以被 Google 拦下。
        # 首次启动时探测真实 UA，改掉 Headless 标记后重启一次，结果缓存起来。
        if self._chrome.user_agent is None:
            fixed = normalize_user_agent(self._chrome.browser_user_agent)
            if fixed:
                logger.debug("UA 含 HeadlessChrome，改写后重启: %s", fixed)
                await asyncio.to_thread(self._chrome.stop)
                write_cached_user_agent(profile, executable, fixed)
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
        await asyncio.to_thread(chrome.start)
        return chrome

    def _bundled_chromium(self) -> str | None:
        """Playwright 默认位置的 Chromium 路径（可能并不存在）。"""
        try:
            return self._playwright.chromium.executable_path
        except Exception:  # noqa: BLE001
            return None

    async def _resolve_executable(self) -> str:
        """定位浏览器；缺失且开了自动安装就先装再找。"""
        cfg = self._config
        bundled = self._bundled_chromium()
        explicit = None if cfg.prefer_bundled_chromium else cfg.chrome_path
        if cfg.prefer_bundled_chromium and bundled:
            explicit = bundled
        install_dir = self.browsers_dir

        path, checked = locate_chrome(explicit, bundled, install_dir)
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
        path, _ = locate_chrome(self._config.chrome_path, None, self.browsers_dir)
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
            logger.debug("清 cookie 失败: %s", exc)
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
            logger.debug("预热失败，忽略: %s", exc)
        finally:
            await page.close()

    async def close(self) -> None:
        async with self._lock:
            await self._teardown()

    async def _teardown(self) -> None:
        if self._context is not None and self._browser is None:
            # launch_persistent_context 拿到的是 context，关它即可
            try:
                await self._context.close()
            except Exception:  # noqa: BLE001
                pass
        self._context = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None
        if self._chrome is not None:
            await asyncio.to_thread(self._chrome.stop)
            self._chrome = None

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
                                 script: str, debug_name: str | None = None,
                                 ) -> tuple[Any, str, str]:
        """在浏览器里上传图片、切到完全匹配页、执行提取脚本。

        为什么上传要在浏览器里做：``vsrid`` 会话绑定在上传方的身份上，
        换别的客户端上传、再让浏览器打开结果页，页面会显示
        "Expired visual search"。而 Playwright 1.5x 之后
        ``context.request`` 已经不再和 CDP 附加的上下文共享会话
        （1.49 时还共享），所以只剩「走真实上传界面」这条稳的路 ——
        它本来也最贴近真实用户行为。

        Returns:
            ``(脚本返回值, 完全匹配页地址, 上传后的结果页地址)``
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
                    f"打开 Lens 上传页失败: {type(exc).__name__}: {exc}") from exc
            self._assert_not_blocked(page.url)
            await page.wait_for_timeout(2000)
            await self._dismiss_consent(page)

            lens_url = await self._submit_image(page, image, filename, mime)
            logger.debug("上传后的结果页: %s", lens_url)

            exact_url = to_exact_matches_url(lens_url, cfg.hl)
            logger.debug("完全匹配页: %s", exact_url)
            try:
                await page.goto(exact_url, wait_until="domcontentloaded",
                                timeout=cfg.timeout_ms)
            except Exception as exc:  # noqa: BLE001
                raise FetchError(
                    f"打开完全匹配页失败: {type(exc).__name__}: {exc}") from exc
            self._assert_not_blocked(page.url)
            await self._settle(page)
            data = await page.evaluate(script)
            if cfg.debug_dir and debug_name:
                await self._dump(page, debug_name)
            return data, exact_url, lens_url
        finally:
            if opened:
                await page.close()
            else:
                try:
                    await page.goto("about:blank")
                except Exception:  # noqa: BLE001
                    pass

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
                logger.debug("file input[%d] 不接受文件: %s", index, exc)
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

            # 等参数补全
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(4000)
            self._assert_not_blocked(page.url)
            return page.url
        raise UploadError(
            "上传后没有跳转到结果页。可能是图片被拒绝，或 Lens 页面结构变了")

    async def _settle(self, page: Any) -> None:
        """等结果渲染完：等网络空闲、再滚几屏把懒加载的卡片带出来。"""
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(self._config.settle_ms)
        for _ in range(3):
            await page.mouse.wheel(0, 2200)
            await page.wait_for_timeout(900)
        self._assert_not_blocked(page.url)

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
                raise FetchError(f"打开结果页失败: {type(exc).__name__}: {exc}") from exc

            self._assert_not_blocked(page.url)
            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(cfg.settle_ms)
            # 结果是懒加载的，滚几屏把后面的卡片带出来
            for _ in range(3):
                await page.mouse.wheel(0, 2200)
                await page.wait_for_timeout(900)
            self._assert_not_blocked(page.url)

            data = await page.evaluate(script)
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
                raise FetchError(f"打开页面失败: {type(exc).__name__}: {exc}") from exc
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

        quiet_http_logs()
        headers = {"User-Agent": self.user_agent or self._config.user_agent}
        if referer:
            headers["Referer"] = referer
        # 跳板只是一次解码重定向，实测 0.6s 就够；给太长的超时只会让
        # 个别卡住的链接拖慢整批
        timeout = min(20.0, self._config.timeout_ms / 1000)
        semaphore = asyncio.Semaphore(concurrency)

        async with httpx.AsyncClient(proxy=self._config.proxy,
                                     follow_redirects=False,
                                     timeout=timeout,
                                     headers=headers) as client:
            async def resolve(url: str) -> str | None:
                async with semaphore:
                    try:
                        resp = await client.get(url)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("还原跳板失败 %s: %s", url[:70], exc)
                        return None
                location = resp.headers.get("location", "")
                if location.startswith(("http://", "https://")):
                    return location
                logger.debug("跳板未返回重定向（status=%s）: %s",
                             resp.status_code, url[:70])
                return None

            return list(await asyncio.gather(*(resolve(u) for u in urls)))

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
