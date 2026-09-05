"""万一还是撞上人机验证时的兜底：开有头浏览器，人工点掉，
豁免 cookie 会写进模块使用的持久化 profile，后续无头运行也能复用。

正常情况用不到 —— CDP 模式（默认）下 Google 不会弹验证。真撞上了先考虑换节点
（``python tools/scan_nodes.py``），换不动再用这个。

    python tools/solve_captcha.py
    python tools/solve_captcha.py --node <节点名> --wait 300
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from image_search import SearchConfig  # noqa: E402
from image_search.browser import BrowserSession  # noqa: E402

TARGET = "https://www.google.com/search?q=hello+world&hl=en"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=240, help="等待人工操作的秒数")
    ap.add_argument("--url", default=TARGET)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--node", default=None, help="先把 Clash 的 Google 组切到该节点")
    ap.add_argument("--group", default="Google")
    args = ap.parse_args()

    original = None
    if args.node:
        from clash_api import get_proxies, switch

        original = get_proxies().get(args.group, {}).get("now")
        switch(args.group, args.node)
        print("已切换到指定代理节点")
        await asyncio.sleep(2)

    config = SearchConfig(headless=False, use_cdp=True, proxy=args.proxy)
    print("浏览器 profile 已准备")

    session = BrowserSession(config)
    await session.start()
    page = await session.context.new_page()
    try:
        await page.goto(args.url, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(3000)

        if "/sorry/" not in page.url:
            print("[+] 没有触发人机验证，当前网络可直接使用")
            return 0

        print(f"[!] 触发了人机验证。请在打开的窗口里完成验证，最多等 {args.wait} 秒 ...")
        waited = 0
        while waited < args.wait * 1000 and "/sorry/" in page.url:
            await page.wait_for_timeout(3000)
            waited += 3000

        if "/sorry/" in page.url:
            print("[-] 仍在验证页。建议换节点：python tools/scan_nodes.py")
            return 1

        names = [c["name"] for c in await session.context.cookies()]
        print("[+] 验证通过，已回到搜索页")
        if "GOOGLE_ABUSE_EXEMPTION" in names:
            print("    验证豁免状态已写入持久化 profile")
        return 0
    finally:
        await page.close()
        await session.close()
        if args.node and original:
            from clash_api import switch

            try:
                switch(args.group, original)
                print("已恢复原代理选择")
            except Exception as exc:  # noqa: BLE001
                print(f"!! 恢复原代理选择失败，请手动恢复："
                      f"{type(exc).__name__}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
