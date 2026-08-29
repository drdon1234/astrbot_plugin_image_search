"""把 AstrBot WebUI 的配置字典翻译成模块自己的配置对象。

隔在这里的好处是 ``image_search`` 其余部分完全不知道 AstrBot 的存在，
本地脚本可以直接构造 :class:`SearchConfig`，插件走这里转换。
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any, Mapping

from .config import SearchConfig
from .formatter import OutputOptions

DEFAULT_COMMAND = "搜图"


def _section(config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = config.get(name)
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _int(value: Any, default: int, minimum: int | None = None,
         maximum: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on", "是", "开"}:
            return True
        if lowered in {"false", "0", "no", "off", "否", "关"}:
            return False
    return default


@dataclasses.dataclass(slots=True)
class PluginOptions:
    """插件层面的行为选项（和搜索本身无关的部分）。

    Attributes:
        command: 触发指令名。固定值，仅用于日志和提示文案 —— AstrBot 的
            ``@filter.command`` 在导入时就确定了，没法跟着配置变。
        wait_image_seconds: 只发指令没带图时，等待用户补图的秒数；0 = 不等待。
        user_cooldown_seconds: 同一用户的冷却秒数；0 = 不限制。
        idle_close_minutes: 浏览器空闲多久后关掉释放内存；0 = 常驻不关。
        working_hint: 开始搜索时的提示语；留空则不提示。
        request_timeout_seconds: 单次搜索的总超时秒数；0 = 不限制。
            这是最后一道保险 —— 底层卡死时保证用户能收到回复。
    """

    command: str = DEFAULT_COMMAND
    wait_image_seconds: int = 60
    user_cooldown_seconds: int = 15
    idle_close_minutes: int = 30
    working_hint: str = "正在搜索，请稍候……"
    request_timeout_seconds: int = 180


@dataclasses.dataclass(slots=True)
class PluginConfig:
    """插件的完整配置。"""

    search: SearchConfig
    output: OutputOptions
    options: PluginOptions


def build_config(raw: Mapping[str, Any] | None,
                 data_dir: pathlib.Path | None = None) -> PluginConfig:
    """从 AstrBot 的配置字典构造出三份配置对象。

    Args:
        raw: AstrBot 注入的配置字典（``_conf_schema.json`` 对应的内容）。
        data_dir: 插件数据目录，浏览器 profile 会放在它下面。
    """
    raw = raw or {}
    search_raw = _section(raw, "search")
    browser_raw = _section(raw, "browser")
    output_raw = _section(raw, "output")
    limits_raw = _section(raw, "limits")

    max_results = _int(search_raw.get("max_results"), 5, minimum=1, maximum=50)
    root = pathlib.Path(data_dir) if data_dir else None
    profile_root = (root / "browser_profile") if root else None
    install_root = (root / "ms-playwright") if root else None

    search = SearchConfig(
        headless=_bool(browser_raw.get("headless"), True),
        chrome_path=_text(browser_raw.get("chrome_path")) or None,
        prefer_bundled_chromium=_bool(
            browser_raw.get("prefer_bundled_chromium"), False),
        auto_install_browser=_bool(browser_raw.get("auto_install_browser"), True),
        install_system_deps=_bool(browser_raw.get("install_system_deps"), True),
        install_timeout_seconds=_int(browser_raw.get("install_timeout_minutes"), 30,
                                     minimum=5, maximum=120) * 60,
        browser_install_dir=install_root,
        user_data_dir=profile_root,
        proxy=_text(browser_raw.get("proxy")) or None,
        hl=_text(search_raw.get("hl"), "en"),
        timeout_ms=_int(browser_raw.get("timeout_seconds"), 60,
                        minimum=15, maximum=300) * 1000,
        max_results=max_results,
        max_retries=_int(browser_raw.get("max_retries"), 2, minimum=0, maximum=5),
        complete_titles=_bool(search_raw.get("complete_titles"), True),
    )

    output = OutputOptions(
        limit=max_results,
        show_source=_bool(output_raw.get("show_source"), True),
        show_size=_bool(output_raw.get("show_size"), False),
        show_index=_bool(output_raw.get("show_index"), True),
        show_ocr=_bool(search_raw.get("with_ocr"), False),
        empty_text=_text(output_raw.get("empty_text"), "没有找到完全匹配的结果"),
    )

    options = PluginOptions(
        command=DEFAULT_COMMAND,
        wait_image_seconds=_int(limits_raw.get("wait_image_seconds"), 60,
                                minimum=0, maximum=300),
        user_cooldown_seconds=_int(limits_raw.get("user_cooldown_seconds"), 15,
                                   minimum=0, maximum=600),
        idle_close_minutes=_int(browser_raw.get("idle_close_minutes"), 30,
                                minimum=0, maximum=1440),
        working_hint=_text(output_raw.get("working_hint"), "正在搜索，请稍候……"),
        request_timeout_seconds=_int(limits_raw.get("request_timeout_seconds"),
                                    180, minimum=0, maximum=1800),
    )

    return PluginConfig(search=search, output=output, options=options)
