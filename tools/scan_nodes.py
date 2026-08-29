"""逐个试 Clash 节点，找出访问 google.com/search 不触发人机验证的那个。

做法：通过 mihomo 命名管道 API 把 ``Google`` 分组临时切到某个节点，用真实
浏览器打开一次搜索页，看是否被转到 ``/sorry/index``。被拦时 /sorry 页面本身
会打印 Google 看到的出口 IP，正好拿来判断节点质量。

脚本结束（含异常/中断）都会把分组恢复成原来的选择。

    python tools/scan_nodes.py
    python tools/scan_nodes.py --group Google --node "US-01"
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from clash_api import get_proxies, switch  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PROBE_URL = "https://www.google.com/search?q=hello+world&hl=en"
SKIP = {"DIRECT", "REJECT", "PASS-RULE", "Proxy", "GLOBAL"}
_IP_RE = re.compile(r"IP address:\s*([\da-fA-F.:]+)")


async def check_node(browser, node: str) -> dict:
    """在当前节点下打开一次搜索页，返回诊断信息。"""
    context = await browser.new_context(
        user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 900})
    await context.add_cookies([{
        "name": "SOCS", "value": "CAESHAgBEhJnd3NfMjAyNDA2MTAtMF9SQzIaAmVuIAEaBgiA_LyaBg",
        "domain": ".google.com", "path": "/"}])
    page = await context.new_page()
    info: dict = {"node": node, "sorry": None, "ip": None, "note": ""}
    try:
        await page.goto(PROBE_URL, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(3500)
        info["sorry"] = "/sorry/" in page.url
        text = await page.inner_text("body")
        match = _IP_RE.search(text)
        if match:
            info["ip"] = match.group(1)
        if not info["sorry"]:
            # 没被拦：确认页面里真的有搜索结果
            html = await page.content()
            info["note"] = (f"h3={html.count('<h3')} "
                            f"results_div={'id=\"search\"' in html}")
    except Exception as exc:  # noqa: BLE001
        info["note"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    finally:
        await context.close()
    return info


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="Google", help="要切换的代理组")
    ap.add_argument("--node", default=None, help="只测这一个节点")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    proxies = get_proxies()
    if args.group not in proxies:
        print(f"没有叫 {args.group} 的代理组")
        return 1
    group = proxies[args.group]
    original = group.get("now")
    candidates = [n for n in (group.get("all") or []) if n not in SKIP]
    if args.node:
        candidates = [args.node]

    print(f"组 {args.group} 当前选中: {original}")
    print(f"待测节点 ({len(candidates)}): {candidates}\n")

    results: list[dict] = []
    async with async_playwright() as p:
        launch: dict = {"headless": not args.headed}
        try:
            browser = await p.chromium.launch(channel="chrome", **launch)
        except Exception:  # noqa: BLE001
            browser = await p.chromium.launch(**launch)
        try:
            for node in candidates:
                try:
                    switch(args.group, node)
                except Exception as exc:  # noqa: BLE001
                    print(f"  {node:<14} 切换失败: {exc}")
                    continue
                await asyncio.sleep(2.0)
                info = await check_node(browser, node)
                results.append(info)
                flag = "被拦" if info["sorry"] else ("可用" if info["sorry"] is False
                                                     else "异常")
                print(f"  {node:<14} {flag:<4} ip={info['ip'] or '-':<16} "
                      f"{info['note']}")
        finally:
            await browser.close()
            if original:
                try:
                    switch(args.group, original)
                    print(f"\n已把 {args.group} 恢复为 {original}")
                except Exception as exc:  # noqa: BLE001
                    print(f"\n!! 恢复 {args.group} 失败，请在 Clash Verge 里手动选回 "
                          f"{original}：{exc}")

    usable = [r["node"] for r in results if r["sorry"] is False]
    print("\n可用节点:", usable or "无")
    return 0 if usable else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
