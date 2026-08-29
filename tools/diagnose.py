"""分段体检：逐步验证反搜链路的每一环，快速定位卡在哪一步。

    python tools/diagnose.py
    python tools/diagnose.py --proxy http://127.0.0.1:7897
    python tools/diagnose.py --headed --playwright-launch   # 对比自动化标记的影响
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from image_search import LensSession, SearchConfig  # noqa: E402
from image_search.browser import BrowserSession  # noqa: E402
from image_search.exceptions import RateLimitedError  # noqa: E402
from image_search.loader import load_image  # noqa: E402
from image_search.parser import EXTRACT_SCRIPT, extract_items  # noqa: E402
from image_search.uploader import to_exact_matches_url  # noqa: E402

ROOT = pathlib.Path(__file__).parent.parent
OK, BAD, WARN = "[ OK ]", "[FAIL]", "[WARN]"
EXPIRED_HINT = "Expired visual search"


async def main() -> int:  # noqa: PLR0911, PLR0915
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=str(ROOT / "test_imgs" / "test.png"))
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--playwright-launch", action="store_true",
                    help="用 Playwright 直接启动浏览器做对比（预期会被拦）")
    ap.add_argument("--bundled", action="store_true",
                    help="强制用 Playwright 自带 Chromium（等同 Docker 环境）")
    args = ap.parse_args()

    config = SearchConfig(
        headless=not args.headed,
        use_cdp=not args.playwright_launch,
        prefer_bundled_chromium=args.bundled,
        proxy=args.proxy,
        debug_dir=ROOT / "tools" / "_dump" / "diagnose",
    )
    print(f"启动方式: {'CDP 附加（普通启动浏览器）' if config.use_cdp else 'Playwright launch'}")

    # 1. 出口 IP
    print("\n1) 出口 IP")
    try:
        import httpx

        async with httpx.AsyncClient(timeout=20, proxy=args.proxy) as client:
            info = (await client.get("https://ipinfo.io/json")).json()
        print(f"   {OK} {info.get('ip')}  {info.get('city')}/{info.get('country')}  "
              f"{info.get('org')}")
    except Exception as exc:  # noqa: BLE001
        print(f"   {BAD} {type(exc).__name__}: {exc}")

    # 2. 图片加载
    print("2) 读取图片")
    try:
        data, name, mime = await load_image(args.image, config)
        print(f"   {OK} {name}  {len(data)} bytes  {mime}")
    except Exception as exc:  # noqa: BLE001
        print(f"   {BAD} {type(exc).__name__}: {exc}")
        return 1

    # 3/4. 纯 HTTP 上传 + OCR（同一会话）
    print("3) 纯 HTTP 上传 lens.google.com/v3/upload")
    async with LensSession(config) as http_session:
        try:
            location = await http_session.upload(data, name, mime)
            print(f"   {OK} 拿到结果页地址（{len(location)} 字符）")
        except Exception as exc:  # noqa: BLE001
            print(f"   {BAD} {type(exc).__name__}: {exc}")
            return 1

        print("4) qfmetadata OCR（必须复用上传的会话）")
        try:
            lines = await http_session.ocr_lines(location)
            if lines:
                print(f"   {OK} 识别到 {len(lines)} 行: {lines[:4]}")
            else:
                print(f"   {WARN} 返回空 —— 图里可能没文字")
        except Exception as exc:  # noqa: BLE001
            print(f"   {BAD} {type(exc).__name__}: {exc}")

    # 5. 启动浏览器
    print("5) 启动浏览器")
    browser = BrowserSession(config)
    try:
        await browser.start()
        print(f"   {OK} 已就绪")
        print(f"        可执行文件: {browser.executable}")
        user_agent = browser.user_agent or ""
        print(f"        生效 UA: {user_agent}")
        if "HeadlessChrome" in user_agent:
            print(f"   {WARN} UA 里还有 HeadlessChrome，会被 Google 拦下")
    except Exception as exc:  # noqa: BLE001
        print(f"   {BAD} {type(exc).__name__}: {exc}")
        return 1

    try:
        # 6. 用浏览器会话重新上传（vsrid 必须属于浏览器，否则结果页显示已过期）
        print("6) 用浏览器网络栈上传")
        browser_session = LensSession(config, browser.context)
        try:
            location = await browser_session.upload(data, name, mime)
            print(f"   {OK} 拿到结果页地址")
        except Exception as exc:  # noqa: BLE001
            print(f"   {BAD} {type(exc).__name__}: {exc}")
            return 1

        # 7. 渲染并抽卡片
        print("7) 渲染 Exact matches 页并抽取卡片")
        exact_url = to_exact_matches_url(location, config.hl)
        try:
            payload = await browser.render_and_extract(exact_url, EXTRACT_SCRIPT,
                                                      debug_name="diagnose")
        except RateLimitedError as exc:
            print(f"   {BAD} 人机验证: {exc}")
            return 3
        except Exception as exc:  # noqa: BLE001
            print(f"   {BAD} {type(exc).__name__}: {exc}")
            return 1

        items = extract_items(payload, config.max_results)
        print(f"   {OK} 页面标题: {payload.get('pageTitle')}")
        print(f"        候选链接 {len(payload.get('items') or [])} 个 -> "
              f"卡片 {len(items)} 条")
        if not items:
            html_path = pathlib.Path(config.debug_dir) / "diagnose.html"
            expired = (EXPIRED_HINT.lower() in
                       html_path.read_text(encoding="utf-8", errors="replace").lower()
                       if html_path.exists() else False)
            if expired:
                print(f"   {BAD} 页面显示 '{EXPIRED_HINT}' —— "
                      f"上传会话和浏览器不一致")
            else:
                print(f"   {WARN} 没抽到卡片，可能确实没有完全匹配结果。"
                      f"调试文件见 {config.debug_dir}")
            return 2

        # 8. 还原跳板链接
        print("8) 还原 /goto 跳板链接")
        gotos = [i.goto for i in items if i.goto and not i.url]
        if gotos:
            locations = await browser.resolve_redirects(
                gotos, referer=exact_url, concurrency=config.resolve_concurrency)
            ok = sum(1 for x in locations if x)
            print(f"   {OK} {ok}/{len(gotos)} 个还原成功")
        else:
            print(f"   {OK} 全是直链，无需还原")

        print("\n前 5 条结果：")
        resolved = await _build(browser, items, exact_url)
        for m in resolved[:5]:
            print(f"  链接: {m.url}")
            print(f"  标题: {m.content}")
            print(f"  来源: {m.source}  {m.width}x{m.height}")
            print()
        return 0 if resolved else 2
    finally:
        await browser.close()


async def _build(browser, items, referer):
    pending = [i for i, raw in enumerate(items) if not raw.url and raw.goto]
    resolved = {}
    if pending:
        locations = await browser.resolve_redirects(
            [items[i].goto or "" for i in pending], referer=referer)
        resolved = dict(zip(pending, locations))
    out = []
    for i, raw in enumerate(items):
        url = raw.url or resolved.get(i)
        if url:
            out.append(raw.to_exact_match(url))
    return out


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
