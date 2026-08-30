"""AstrBot 插件入口：Google Lens 反向搜图。

用法（指令 ``搜图``，别名 ``以图搜图`` / ``soutu`` / ``sauce``，``/`` 前缀可选）：

* ``搜图`` 并在同一条消息里带图片
* 引用一条带图片的消息，回复 ``搜图``
* 只发 ``搜图``，然后在超时时间内补发图片

群聊和私聊都不需要 @ 机器人。触发条件走正则而不是 ``@filter.command``，
原因见 :data:`TRIGGER_PATTERN` 上方的注释。

搜索逻辑全在 ``image_search`` 包里，这里只负责：取图、调服务、拼消息、
管生命周期。
"""

from __future__ import annotations

import asyncio
import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain, Reply
from astrbot.api.star import Context, Star, StarTools, register

from .image_search.exceptions import (
    BrowserNotAvailableError,
    ImageSearchError,
    RateLimitedError,
)
from .image_search.formatter import format_blocks, format_result
from .image_search.logger import logger
from .image_search.models import LensSearchResult
from .image_search.plugin_config import build_config
from .image_search.service import LensSearchService

PLUGIN_NAME = "astrbot_plugin_image_search"

# 触发条件用正则而不是 @filter.command，否则群聊里根本进不来。CommandFilter 有
# 两道卡口（AstrBot 4.27 实测）：
#
# 1. ``if not event.is_at_or_wake_command: return False``。这个标志只在 @ 机器人、
#    带 wake_prefix、引用机器人自己的消息、或私聊时才为真。而最常用的形态是
#    「引用群友发的图 + 搜图」，既没 @ 也没前缀，标志为假，指令直接被跳过。
# 2. 要求 ``message_str`` 以指令名开头。群里 @ 机器人时，message_str 前面可能
#    还留着「@昵称(QQ号)」，开头对不上。
#
# RegexFilter 两条都不受约束（源码注释写明「不会受到 wake_prefix 的制约」），而且
# filter 通过本身就会把事件标记为已唤醒，所以群聊里不 @ 也能触发。
# 代价是要自己写全别名和前缀，并且用 ^...$ 收紧边界，避免聊天里提到「搜图」就误触发。

#: 群聊 @ 机器人后 message_str 里可能残留的提及文本，形如「@昵称(QQ号) 」
_MENTION_PREFIX = r"(?:\s*@[^@]*?\(\d+\)\s*)*"
#: 可选的指令前缀，全角半角都认
_COMMAND_PREFIX = r"\s*[/／]?\s*"

#: 触发搜图。``(?i)`` 让英文别名不区分大小写，对中文无影响
TRIGGER_PATTERN = (
    rf"(?i)^{_MENTION_PREFIX}{_COMMAND_PREFIX}"
    r"(?:搜图|以图搜图|soutu|sauce)\s*$")
#: 查看浏览器状态（管理员）
STATUS_PATTERN = (
    rf"(?i)^{_MENTION_PREFIX}{_COMMAND_PREFIX}"
    r"(?:搜图状态|soutu_status)\s*$")


@register(
    PLUGIN_NAME,
    "drdon1234",
    "用 Google Lens 反向搜图，解析完全匹配结果并返回来源链接与标题",
    "0.1.0",
)
class ImageSearchPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = build_config(config, data_dir=self._data_dir())
        self.service = LensSearchService(
            self.config.search,
            idle_close_seconds=self.config.options.idle_close_minutes * 60,
        )
        self._cooldown: dict[str, float] = {}
        self._prepare_task: asyncio.Task[None] | None = None
        logger.info(
            "%s 已加载：指令=%s，最多返回 %d 条，浏览器空闲 %d 分钟后关闭",
            PLUGIN_NAME,
            self.config.options.command,
            self.config.search.max_results,
            self.config.options.idle_close_minutes,
        )
        self._schedule_prepare()

    # -- 浏览器预备 ---------------------------------------------------------
    def _schedule_prepare(self) -> None:
        """后台补齐浏览器。

        AstrBot 装插件只会装 pip 依赖，不会执行 ``playwright install``，所以
        官方镜像里浏览器二进制是缺的。这里在插件加载后丢到后台下载，避免用户
        第一次搜图时干等几分钟。不阻塞加载，失败也只记日志。
        """
        if self._prepare_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有运行中的事件循环，交给 on_astrbot_loaded 或首次搜索时兜底
            logger.debug("插件加载时没有事件循环，浏览器预备延后")
            return
        self._prepare_task = loop.create_task(self._prepare_browser())

    async def _prepare_browser(self) -> None:
        try:
            if self.service.browser_ready():
                logger.info("浏览器已就绪")
                return
            logger.info("浏览器二进制缺失，开始后台自动安装")
            await self.service.prepare()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("后台准备浏览器失败: %s", exc)

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """AstrBot 启动完成后再兜一次，覆盖插件加载时没有事件循环的情况。"""
        self._schedule_prepare()

    @staticmethod
    def _data_dir():
        """插件数据目录，浏览器 profile 放在这里，随 AstrBot 数据一起持久化。"""
        try:
            return StarTools.get_data_dir(PLUGIN_NAME)
        except Exception as exc:  # noqa: BLE001
            logger.debug("取插件数据目录失败，用默认位置: %s", exc)
            return None

    async def terminate(self) -> None:
        task = self._prepare_task
        self._prepare_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.service.close()
        logger.info("%s 已卸载，浏览器已关闭", PLUGIN_NAME)

    # -- 指令 ---------------------------------------------------------------
    @filter.regex(TRIGGER_PATTERN)
    async def search_image(self, event: AstrMessageEvent):
        """用 Google Lens 反搜图片来源。

        全程用 ``event.send()`` 主动发送，不走 ``yield``。两个原因：

        1. 结果可能要分成多条消息，而 AstrBot 的 pipeline 在每个 ``yield``
           之间都会检查 ``event.is_stopped()``，一旦事件被终止，后面 yield
           出去的内容就直接丢了。主动发送不受这个状态影响。
        2. ``yield`` 出去的结果要经过 ``result_decorate`` 阶段，那里会按全局
           配置 ``platform_settings.forward_threshold``（默认 1500 字）把长消息
           折叠成合并转发，还会按 ``reply_with_quote`` 加引用。搜图结果的字数
           随描述长短和条数浮动，正好在阈值附近来回，于是同一个指令时而被折叠
           时而不被折叠。``event.send()`` 直连平台适配器，绕过整个装饰阶段，
           要不要合并转发完全由本插件的配置说了算。
        """
        limited = self._check_cooldown(event)
        if limited:
            await event.send(event.plain_result(limited))
            return

        # 浏览器还在后台装的时候，给个明确的进度而不是一句失败
        if not self.service.browser_ready():
            self._schedule_prepare()
            await event.send(event.plain_result(
                f"{self.service.install_status()}\n装好后再发一次就行。"))
            return

        image = self._pick_image(event)
        if image is None:
            image = await self._wait_for_image(event)
            if image is None:
                return

        hint = self.config.options.working_hint
        if hint:
            await event.send(event.plain_result(hint))

        try:
            payload = await image.convert_to_file_path()
        except Exception as exc:  # noqa: BLE001
            logger.warning("获取图片失败: %s", exc)
            await event.send(event.plain_result("拿不到这张图片，换一张再试试"))
            return

        outcome = await self._search(payload)
        if isinstance(outcome, str):
            await event.send(event.plain_result(outcome))
            return
        await self._send_result(event, outcome)

    async def _send_result(self, event: AstrMessageEvent,
                           result: LensSearchResult) -> None:
        """按配置把结果发出去：一条合并转发，或者逐块发普通消息。"""
        blocks = format_blocks(result, self.config.output)
        if self.config.output.use_forward_message:
            name, uin = self._forward_identity(event)
            nodes = [Node(name=name, uin=uin, content=[Plain(block)])
                     for block in blocks]
            try:
                await event.send(event.chain_result([Nodes(nodes)]))
                return
            except Exception as exc:  # noqa: BLE001
                # 合并转发是 QQ 特有的，其它平台会失败。回退成普通消息，
                # 总比什么都收不到好。
                logger.warning("合并转发发送失败，回退为普通消息: %s", exc)
        for block in blocks:
            await event.send(event.plain_result(block))

    def _forward_identity(self, event: AstrMessageEvent) -> tuple[str, str]:
        """合并转发里每个节点显示的发送者。取不到 id 就用占位值。"""
        try:
            uin = str(event.get_self_id() or "0")
        except Exception as exc:  # noqa: BLE001
            logger.debug("取机器人自身 id 失败: %s", exc)
            uin = "0"
        return self.config.options.command, uin

    # -- 内部实现 -----------------------------------------------------------
    async def _run_search(self, image_path: str) -> str:
        """搜索并把结果拼成一段文本。给 LLM 工具和校验脚本用。"""
        outcome = await self._search(image_path)
        if isinstance(outcome, str):
            return outcome
        return format_result(outcome, self.config.output)

    async def _search(self, image_path: str) -> LensSearchResult | str:
        """执行搜索。成功返回结果对象，失败返回可直接发送的错误文案。

        整个搜索套了一层总超时。底层卡死时用户必须能收到明确回复 —— 实测过一种
        情况：AstrBot 运行期间 pip 升级了 playwright，旧客户端配新 driver 会让
        Playwright 的连接层静默挂死，我们传给它的 timeout 一概无效，
        用户只收到「正在搜索」就再也没有下文。
        """
        timeout = self.config.options.request_timeout_seconds
        search = self.service.search(
            image_path, with_ocr=self.config.output.show_ocr)
        try:
            if timeout > 0:
                result = await asyncio.wait_for(search, timeout)
            else:
                result = await search
        except asyncio.TimeoutError:
            logger.error("搜索超过 %d 秒没有返回，强制关闭浏览器会话", timeout)
            await self._force_close()
            return (f"搜索超时（{timeout} 秒无响应），已重置浏览器会话。\n"
                    "请稍后重试。若反复出现，请让管理员查看日志，"
                    "并确认升级过依赖后重启了 AstrBot。")
        except RateLimitedError:
            logger.warning("Google 人机验证，重试后仍未通过")
            return ("Google 触发了人机验证，暂时搜不了。稍后再试，"
                    "或让管理员换个代理节点。")
        except BrowserNotAvailableError as exc:
            logger.error("浏览器不可用: %s", exc)
            return f"浏览器启动失败，请让管理员检查部署环境：\n{exc}"
        except ImageSearchError as exc:
            logger.warning("搜索失败: %s: %s", type(exc).__name__, exc)
            return f"搜索失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("搜图出现未预期的错误: %s", exc)
            return "搜索出错了，详情见 AstrBot 日志"
        return result

    async def _force_close(self) -> None:
        """强制释放浏览器会话，让下一次搜索从干净状态重新开始。

        底层卡死时 ``close()`` 自己也可能卡（它同样要等 Playwright 响应），
        所以再套一层超时；实在关不掉就只记日志，至少不要把这次请求也拖住。
        """
        try:
            await asyncio.wait_for(self.service.close(), timeout=20)
        except asyncio.TimeoutError:
            logger.error("关闭浏览器会话同样超时，可能需要重启 AstrBot")
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭浏览器会话失败: %s", exc)

    def _check_cooldown(self, event: AstrMessageEvent) -> str:
        """返回非空字符串表示还在冷却中。"""
        seconds = self.config.options.user_cooldown_seconds
        if seconds <= 0:
            return ""
        key = event.unified_msg_origin + "|" + str(event.get_sender_id())
        now = time.monotonic()
        last = self._cooldown.get(key, 0.0)
        remaining = seconds - (now - last)
        if remaining > 0:
            return f"搜图冷却中，还需 {remaining:.0f} 秒"
        self._cooldown[key] = now
        # 顺手清掉过期记录，避免长期运行后字典无限增长
        if len(self._cooldown) > 256:
            cutoff = now - seconds
            self._cooldown = {k: v for k, v in self._cooldown.items() if v > cutoff}
        return ""

    @staticmethod
    def _images_in(components) -> list[Image]:
        return [c for c in (components or []) if isinstance(c, Image)]

    def _pick_image(self, event: AstrMessageEvent) -> Image | None:
        """从当前消息或被引用的消息里取第一张图片。"""
        messages = event.get_messages() or []
        images = self._images_in(messages)
        if images:
            return images[0]
        for component in messages:
            if isinstance(component, Reply):
                replied = self._images_in(component.chain)
                if replied:
                    return replied[0]
        return None

    async def _wait_for_image(self, event: AstrMessageEvent) -> Image | None:
        """指令没带图时，等用户补发一张。等不到或不支持就返回 None。"""
        seconds = self.config.options.wait_image_seconds
        if seconds <= 0:
            await event.send(event.plain_result("请在指令里带上图片，或引用一条图片消息"))
            return None

        try:
            from astrbot.core.utils.session_waiter import (
                SessionController,
                session_waiter,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("当前 AstrBot 版本不支持 session_waiter: %s", exc)
            await event.send(event.plain_result("请在指令里带上图片，或引用一条图片消息"))
            return None

        await event.send(event.plain_result(f"请在 {seconds} 秒内发送要搜索的图片"))
        picked: list[Image] = []

        @session_waiter(timeout=seconds)
        async def waiter(controller: SessionController, next_event: AstrMessageEvent):
            images = self._images_in(next_event.get_messages())
            if not images:
                controller.keep(timeout=seconds, reset_timeout=True)
                return
            picked.append(images[0])
            # 只终止补图那条消息的传播，别让它再去触发别的插件。
            #
            # 这里绝对不能动原来那个指令事件：``stop_event()`` 会把
            # ``_force_stopped`` 永久置位，而 AstrBot 的 pipeline 在每次
            # ``yield`` 之后都检查 ``is_stopped()``，一旦置位，后面产出的结果
            # 就再也发不出去 —— 表现为用户只收到「正在搜索」，然后没有下文。
            next_event.stop_event()
            controller.stop()

        try:
            await waiter(event)
        except TimeoutError:
            await event.send(event.plain_result("等待超时，已取消搜图"))
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("等待图片时出错: %s", exc)
            return None

        return picked[0] if picked else None

    @filter.regex(STATUS_PATTERN)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def search_status(self, event: AstrMessageEvent):
        """查看浏览器状态，缺失时触发安装。"""
        ready = self.service.browser_ready()
        output = self.config.output
        lines = [
            f"浏览器: {'已就绪' if ready else '缺失'}",
            f"安装状态: {self.service.install_status()}",
            f"浏览器进程: {'运行中' if self.service.running else '未启动'}",
            f"代理: {self.config.search.proxy or '未配置（直连）'}",
            f"自动安装: {'开' if self.config.search.auto_install_browser else '关'}",
            f"合并转发: {'开' if output.use_forward_message else '关'}"
            f"　链接单独成条: {'开' if output.link_as_separate_message else '关'}"
            f"　描述与结果合并: {'开' if output.merge_ai_and_exact else '关'}",
        ]
        if not ready:
            self._schedule_prepare()
            lines.append("已触发后台安装，稍后再查。")
        await event.send(event.plain_result("\n".join(lines)))

    # -- 给 LLM 用的函数工具 -------------------------------------------------
    @filter.llm_tool(name="reverse_image_search")
    async def reverse_image_search(self, event: AstrMessageEvent, image_url: str):
        """Reverse image search a picture with Google Lens and return the web pages
        that contain the exact same image.

        Args:
            image_url(string): 图片的 http(s) 地址
        """
        url = (image_url or "").strip()
        if not url.startswith(("http://", "https://")):
            return "image_url 需要是 http(s) 图片地址"
        return await self._run_search(url)
