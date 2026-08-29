"""本地调试脚本：不启动 AstrBot，直接验证 Google Lens 反搜链路。

用法：改下面「配置区」的常量，然后在 IDE 里直接运行本文件（不需要命令行参数）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import sys

# ===========================================================================
# 配置区：改这里，然后直接运行本文件
# ===========================================================================

#: 要搜的图片。相对路径按本文件所在目录解析，也可以直接写 http(s) 图片地址。
IMAGE = "test_imgs/test.png"

#: 运行模式：
#:   "search"  完整搜 exact matches（默认）
#:   "plugin"  走插件的配置映射和输出格式，模拟机器人里的实际效果
#:   "ocr"     只做 OCR，返回图里识别出的文字（纯 HTTP，不启浏览器）
#:   "upload"  只测上传链路，打印结果页地址（纯 HTTP，不启浏览器）
MODE = "search"

#: 最多返回几条结果
LIMIT = 10

#: 是否要「完全匹配」结果（收录该图的网页列表）
EXACT_MATCHES = True

#: 是否要「AI 模式」的图片描述。会多花 10 秒左右。
#: 两个都设 False 会直接报错——那样没有任何可返回的内容。
AI_MODE = True

#: 是否开启安全搜索过滤。默认关闭，开启后命中过滤的图片会返回 0 条结果。
SAFE_SEARCH = False

#: 结果页语言，影响标题和站点名的语言，不影响匹配结果
HL = "en"

#: 是否抓目标页 <title> 补全被 Google 截断的标题。
#: 尽力而为：不少站点有反爬会失败，失败时保留截断标题，并且会慢一些。
COMPLETE_TITLES = False

#: 代理地址，如 "http://127.0.0.1:7897"。None 表示走系统代理或直连。
PROXY = None

#: 是否显示浏览器窗口。调试时打开能直接看到页面长什么样。
HEADED = False

#: 是否把渲染后的 HTML 和截图落到 tools/_dump/debug。
#: 没解析到结果时先看这里的截图，能立刻分辨是 Google 没给结果还是解析规则失效。
DEBUG = False

#: 是否打印详细日志
VERBOSE = False

#: 是否以 JSON 输出（方便管道处理）
OUTPUT_JSON = False

#: 强制用 Playwright 自带的 Chromium，等效模拟 Docker 环境（那里没有系统 Chrome）
USE_BUNDLED_CHROMIUM = False

#: 让 Playwright 自己启动浏览器。预期会被 Google 拦，只用于复现对比，平时别开。
PLAYWRIGHT_LAUNCH = False

# ===========================================================================
# 以下不用改
# ===========================================================================

_ROOT = pathlib.Path(__file__).parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from image_search import GoogleLensSearcher, SearchConfig  # noqa: E402
from image_search.exceptions import ImageSearchError, RateLimitedError  # noqa: E402
from image_search.formatter import OutputOptions, format_result  # noqa: E402
from image_search.plugin_config import build_config  # noqa: E402

VALID_MODES = ("search", "plugin", "ocr", "upload")


def resolve_image(value: str) -> str:
    """相对路径按脚本所在目录解析，避免 IDE 的工作目录不同导致找不到文件。"""
    text = str(value).strip()
    if text.startswith(("http://", "https://")):
        return text
    path = pathlib.Path(text)
    if not path.is_absolute():
        path = _ROOT / path
    return str(path)


def build_search_config() -> SearchConfig:
    return SearchConfig(
        headless=not HEADED,
        use_cdp=not PLAYWRIGHT_LAUNCH,
        prefer_bundled_chromium=USE_BUNDLED_CHROMIUM,
        proxy=PROXY,
        hl=HL,
        max_results=LIMIT,
        exact_matches=EXACT_MATCHES,
        ai_mode=AI_MODE,
        safe_search=SAFE_SEARCH,
        complete_titles=COMPLETE_TITLES,
        debug_dir=(_ROOT / "tools" / "_dump" / "debug") if DEBUG else None,
    )


def build_plugin_config():
    """用 AstrBot 那份配置字典的结构走一遍映射，验证插件的配置链路。"""
    raw = {
        "search": {
            "max_results": LIMIT,
            "hl": HL,
            "exact_matches": EXACT_MATCHES,
            "ai_mode": AI_MODE,
            "safe_search": SAFE_SEARCH,
            "complete_titles": COMPLETE_TITLES,
            "with_ocr": True,
        },
        "browser": {
            "headless": not HEADED,
            "prefer_bundled_chromium": USE_BUNDLED_CHROMIUM,
            "proxy": PROXY or "",
            "timeout_seconds": 60,
            "max_retries": 2,
            "idle_close_minutes": 30,
        },
        "output": {"show_source": True, "show_size": True, "show_index": True},
        "limits": {"user_cooldown_seconds": 15, "wait_image_seconds": 60},
    }
    config = build_config(raw, data_dir=_ROOT / "tools" / "_dump" / "plugin_data")
    if DEBUG:
        config.search.debug_dir = _ROOT / "tools" / "_dump" / "debug"
    return config


async def run() -> int:
    if MODE not in VALID_MODES:
        print(f"MODE 只能是 {VALID_MODES} 之一，当前是 {MODE!r}", file=sys.stderr)
        return 1

    image = resolve_image(IMAGE)
    if not image.startswith(("http://", "https://")) and not pathlib.Path(image).is_file():
        print(f"找不到图片: {image}", file=sys.stderr)
        return 1

    if MODE == "plugin":
        plugin_config = build_plugin_config()
        search_config = plugin_config.search
        output_options = plugin_config.output
    else:
        search_config = build_search_config()
        output_options = OutputOptions(limit=LIMIT, show_size=True, show_ocr=True)

    print(f"模式: {MODE}   图片: {image}")
    if MODE in ("search", "plugin"):
        print(f"浏览器: {'有头' if HEADED else '无头'}"
              f"{'（Playwright 启动，预期会被拦）' if PLAYWRIGHT_LAUNCH else ''}"
              f"{'（自带 Chromium）' if USE_BUNDLED_CHROMIUM else ''}"
              f"   代理: {PROXY or '系统默认'}")
        print(f"profile: {search_config.resolved_user_data_dir()}")
        if MODE == "plugin":
            options = plugin_config.options
            print(f"插件参数: 指令={options.command}  "
                  f"冷却={options.user_cooldown_seconds}s  "
                  f"空闲关闭={options.idle_close_minutes}min")
    print()

    searcher = GoogleLensSearcher(search_config)
    try:
        if MODE == "upload":
            print("结果页地址:")
            print(await searcher.upload(image))
            return 0

        if MODE == "ocr":
            lines = await searcher.ocr(image)
            if OUTPUT_JSON:
                print(json.dumps(lines, ensure_ascii=False, indent=2))
            elif lines:
                print("OCR 文字：")
                for line in lines:
                    print(" ", line)
            else:
                print("没有识别到文字")
            return 0

        result = await searcher.search(image, with_ocr=output_options.show_ocr)
        if OUTPUT_JSON:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0

        print(f"结果页: {result.result_url}\n")
        print(format_result(result, output_options))
        if not result.exact_matches:
            if search_config.debug_dir:
                print(f"\n调试文件已写入: {search_config.debug_dir}")
            else:
                print("\n提示：把 DEBUG 改成 True 再跑一次，可以看到渲染后的页面截图")
            return 2
        return 0
    except RateLimitedError as exc:
        print(f"[被限流] {exc}", file=sys.stderr)
        return 3
    except ImageSearchError as exc:
        print(f"[失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await searcher.close()


def main() -> int:
    logging.basicConfig(
        level=logging.DEBUG if VERBOSE else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
