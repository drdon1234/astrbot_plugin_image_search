"""Google Lens 反搜编排。

三段流程：

1. **上传**：在浏览器里通过真实的 Lens 上传界面提交图片，Google 自己跳到
   带 ``vsrid`` 的结果页。
2. **渲染结果页**：把地址的 ``udm`` 改成 48（Exact matches），等渲染完在页面里
   抽出卡片。
3. **还原链接**：卡片上只有 ``/goto?url=<不透明编码>`` 跳板，逐个请求读 302
   的 ``Location`` 才能得到真实地址。

三个必须遵守的约束（都是实测踩出来的）：

* **上传必须在浏览器里做**。``vsrid`` 会话绑定在上传方身份上，换别的客户端
  上传、再让浏览器打开结果页，页面会显示 "Expired visual search"。
  Playwright 1.49 时 ``context.request`` 还和浏览器共享会话，1.5x 之后不再共享，
  所以只剩「走真实上传界面」这条稳的路。
* **浏览器必须是普通启动 + CDP 附加**，不能让 Playwright 自己 launch，
  否则 botguard 判定为自动化，直接跳 ``/sorry/index``。详见
  :mod:`image_search.chrome`。
* **浏览器版本不能太旧**。实测同一出口 IP 交替对比，Chromium 131 是 0/5，
  Chrome for Testing 151 是 5/5，所以 ``requirements.txt`` 里 playwright
  的版本很关键。
* **还原链接不能用 ``context.request``**。CDP 附加的上下文不会把浏览器的
  代理设置转给它，请求由 Playwright 进程直连发出。容器里没有系统代理时
  会全部超时，而没还原出地址的条目会被丢掉 —— 症状是「抽到 20 张卡片却
  返回 0 条结果」。所以改走 httpx + ``config.proxy``，详见
  :meth:`BrowserSession.resolve_redirects`。
"""

from __future__ import annotations

import asyncio

from .browser import BrowserSession
from .config import SearchConfig
from .exceptions import FetchError, ParseError, RateLimitedError
from .loader import ImageInput, load_image
from .logger import exception_for_log, logger
from .models import ExactMatch, LensSearchResult
from .parser import (
    AI_EXTRACT_SCRIPT,
    EXTRACT_SCRIPT,
    RawMatch,
    ai_html_to_text,
    candidate_limit,
    extract_items,
)
from .session import LensSession
from .titles import complete_titles


class GoogleLensSearcher:
    """Google Lens 图片反搜器。

    浏览器实例在多次搜索间复用 —— 冷启动既慢，也更容易触发人机验证。

    Example:
        >>> async with GoogleLensSearcher() as searcher:
        ...     result = await searcher.search("/path/to/image.png")
        ...     print(result.format())
    """

    def __init__(self, config: SearchConfig | None = None) -> None:
        self.config = config or SearchConfig()
        self._browser = BrowserSession(self.config)
        self._lock = asyncio.Lock()

    # -- 生命周期 -----------------------------------------------------------
    async def start(self) -> None:
        """预热浏览器。不调也行，首次 search 会自动启动。"""
        await self._browser.start()

    async def close(self) -> None:
        await self._browser.close()

    async def __aenter__(self) -> GoogleLensSearcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- 纯 HTTP 能力（不启浏览器） -----------------------------------------
    async def upload(self, image: ImageInput) -> str:
        """上传图片，返回 Lens 结果页地址（``udm`` 未改写）。

        纯 HTTP，不启动浏览器。这个会话拿不到结果页（会显示已过期），
        只适合验证上传接口连通性，以及配合 :meth:`ocr`。
        """
        data, name, mime = await load_image(image, self.config)
        async with LensSession(self.config) as session:
            return await session.upload(data, name, mime)

    async def ocr(self, image: ImageInput) -> list[str]:
        """只做 OCR，返回图片里识别出的文字行。纯 HTTP，不启动浏览器。"""
        data, name, mime = await load_image(image, self.config)
        async with LensSession(self.config) as session:
            location = await session.upload(data, name, mime)
            return await session.ocr_lines(location)

    # -- 完整搜索 -----------------------------------------------------------
    async def search(self, image: ImageInput, *,
                     with_ocr: bool = False) -> LensSearchResult:
        """反搜一张图，返回 Exact matches 结果。

        Args:
            image: 本地路径、图片 URL 或图片字节。
            with_ocr: 是否顺带取一次 OCR 文字（复用同一会话）。

        Raises:
            UploadError: 上传失败。
            RateLimitedError: 命中 Google 人机验证。
            ParseError: 页面结构无法解析。
        """
        async with self._lock:
            data, name, mime = await load_image(image, self.config)
            await self._browser.start()

            # Google 的人机验证判定带随机性，被拦时重新上传再试往往就过了
            attempts = max(1, self.config.max_retries + 1)
            last_error: RateLimitedError | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return await self._search_once(data, name, mime, with_ocr)
                except RateLimitedError as exc:
                    last_error = exc
                    if attempt >= attempts:
                        break
                    logger.debug("第 %d 次撞上人机验证，清会话后 %.1fs 重试",
                                 attempt, self.config.retry_delay_s)
                    # 被标记的 cookie 会一直失败，必须换干净会话
                    await self._browser.reset_session()
                    await asyncio.sleep(self.config.retry_delay_s)
            assert last_error is not None
            raise last_error

    async def _search_once(self, data: bytes, name: str, mime: str,
                           with_ocr: bool) -> LensSearchResult:
        cfg = self.config
        # 两个模式都关掉就没有可做的事了，当成配置错误挡在这里，
        # 不然会白跑一次上传却什么都不返回
        if not cfg.exact_matches and not cfg.ai_mode:
            raise ParseError(
                "「完全匹配」和「AI 模式」都被关闭了，没有可返回的结果；"
                "请至少开启一项")

        # 上传只做一次，两个标签页共用同一个 vsrid —— vsrid 会话绑定上传方
        # 身份，换客户端上传会让结果页显示 Expired visual search
        outcome = await self._browser.upload_and_extract(
            data, name, mime,
            exact_script=EXTRACT_SCRIPT if cfg.exact_matches else None,
            ai_script=AI_EXTRACT_SCRIPT if cfg.ai_mode else None,
            debug_name="lens")

        payload = outcome.exact_payload
        exact_error = outcome.exact_error
        if (cfg.exact_matches and not isinstance(payload, dict)
                and exact_error is None):
            exact_error = ParseError(
                f"完全匹配页面脚本返回了意外类型: {type(payload).__name__}")

        ai_summary = ""
        ai_error = outcome.ai_error
        if isinstance(outcome.ai_payload, dict):
            ai_summary = ai_html_to_text(outcome.ai_payload.get("html") or "")
            logger.debug("AI 描述 %d 字", len(ai_summary))
        if cfg.ai_mode and not ai_summary and ai_error is None:
            ai_error = ParseError(
                "AI 模式没有返回可用正文，可能尚未生成、拒绝识别或页面结构已变化")

        if cfg.ai_mode and not cfg.exact_matches and ai_error is not None:
            raise ai_error
        if cfg.exact_matches and not cfg.ai_mode and exact_error is not None:
            raise exact_error
        if (cfg.exact_matches and cfg.ai_mode and exact_error is not None
                and not ai_summary):
            detail = f"完全匹配：{exact_error}"
            if ai_error is not None:
                detail += f"；AI 模式：{ai_error}"
            raise FetchError(f"两种搜索模式均未成功。{detail}")

        matches: list[ExactMatch] = []
        raw_count = 0
        ocr_task = (asyncio.create_task(self._fetch_ocr(data, name, mime))
                    if with_ocr else None)
        if isinstance(payload, dict):
            raw_items = extract_items(payload, candidate_limit(cfg.max_results))
            raw_count = len(raw_items)
            try:
                matches = await self._resolve(raw_items, outcome.exact_url)
            except RateLimitedError:
                if ocr_task is not None:
                    ocr_task.cancel()
                    await asyncio.gather(ocr_task, return_exceptions=True)
                raise
            except FetchError as exc:
                exact_error = exc
                if not ai_summary:
                    if ocr_task is not None:
                        ocr_task.cancel()
                        await asyncio.gather(ocr_task, return_exceptions=True)
                    raise
                logger.warning("完全匹配链接还原失败，返回 AI 模式结果: %s",
                               exception_for_log(exc))
            except BaseException:
                if ocr_task is not None:
                    ocr_task.cancel()
                    await asyncio.gather(ocr_task, return_exceptions=True)
                raise
            matches = matches[:cfg.max_results]
            logger.debug("候选 %d 条，还原出 %d 条", raw_count, len(matches))

            try:
                if cfg.complete_titles and matches:
                    filled = await complete_titles(matches, cfg)
                    logger.debug("补全了 %d 条标题", filled)
            except BaseException:
                if ocr_task is not None:
                    ocr_task.cancel()
                    await asyncio.gather(ocr_task, return_exceptions=True)
                raise

        try:
            ocr_text = await ocr_task if ocr_task is not None else ""
        except BaseException:
            if ocr_task is not None and not ocr_task.done():
                ocr_task.cancel()
                await asyncio.gather(ocr_task, return_exceptions=True)
            raise

        exact_succeeded = (cfg.exact_matches and isinstance(payload, dict)
                           and exact_error is None)
        if cfg.exact_matches and cfg.ai_mode:
            if exact_error is not None:
                logger.warning("完全匹配失败，已返回 AI 模式的部分结果: %s",
                               exception_for_log(exact_error))
            elif ai_error is not None and exact_succeeded:
                logger.warning("AI 模式失败，已返回完全匹配的部分结果: %s",
                               exception_for_log(ai_error))

        return LensSearchResult(
            exact_matches=matches,
            ai_summary=ai_summary,
            result_url=(outcome.exact_url if exact_succeeded else outcome.ai_url),
            lens_url=outcome.lens_url,
            ocr_text=ocr_text,
        )

    async def _fetch_ocr(self, data: bytes, name: str, mime: str) -> str:
        """独立完成 OCR；调用方可让它与跳板还原安全重叠。"""
        try:
            async with LensSession(self.config) as http_session:
                location = await http_session.upload(data, name, mime)
                return "\n".join(await http_session.ocr_lines(location))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("OCR 获取失败，忽略: %s", exception_for_log(exc))
            return ""

    async def _resolve(self, raw_items: list[RawMatch],
                       referer: str) -> list[ExactMatch]:
        """把跳板链接还原成真实地址，拼出最终结果。"""
        pending = [i for i, raw in enumerate(raw_items) if not raw.url and raw.goto]
        resolved: dict[int, str | None] = {}
        if pending:
            targets = [raw_items[i].goto or "" for i in pending]
            locations = await self._browser.resolve_redirects(
                targets, referer=referer,
                concurrency=self.config.resolve_concurrency)
            resolved = dict(zip(pending, locations))

        matches: list[ExactMatch] = []
        for i, raw in enumerate(raw_items):
            url = raw.url or resolved.get(i)
            if not url:
                continue
            matches.append(raw.to_exact_match(url))
        if raw_items and not matches:
            raise FetchError(
                f"页面抽取到 {len(raw_items)} 条候选，但所有来源链接都还原失败；"
                "请检查代理或 Google 跳板访问是否正常")
        return matches


async def search_image(image: ImageInput,
                       config: SearchConfig | None = None) -> LensSearchResult:
    """一次性搜索的便捷函数。频繁调用请复用 :class:`GoogleLensSearcher`。"""
    searcher = GoogleLensSearcher(config)
    try:
        return await searcher.search(image)
    finally:
        await searcher.close()
