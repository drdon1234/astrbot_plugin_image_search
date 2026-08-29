"""在新环境里逐个组合验证浏览器能不能过 Google 的 botguard。

换机器、换镜像、Playwright 升级后 Chromium 版本变了，都可以跑这个确认。
组合维度：浏览器（自带 Chromium / 系统 Chrome）× 无头 × UA 是否去掉
HeadlessChrome × 是否先访问首页预热。

每个组合都用独立的全新 profile，互不影响。

    python tools/check_browsers.py
    python tools/check_browsers.py --only bundled_headless_ua
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from image_search.chrome import find_chrome, free_port  # noqa: E402

CACHE = pathlib.Path.home() / ".cache" / "astrbot_image_search"
_IP_RE = re.compile(r"IP address:\s*([\da-fA-F.:]+)")
PROBE_URL = "https://www.google.com/search?q=hello+world&hl=en"
NORMAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


async def bundled_path() -> str | None:
    async with async_playwright() as p:
        try:
            return p.chromium.executable_path
        except Exception:  # noqa: BLE001
            return None


async def check(executable: str, tag: str, *, headless: bool = True,
                override_ua: bool = False, warmup: bool = True,
                fresh: bool = True) -> dict:
    profile = CACHE / f"probe_{tag}"
    if fresh and profile.exists():
        shutil.rmtree(profile, ignore_errors=True)
    profile.mkdir(parents=True, exist_ok=True)
    port = free_port()

    args = [executable, f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--lang=en-US",
            "--window-size=1920,1080"]
    if headless:
        args.append("--headless=new")
    if override_ua:
        args.append(f"--user-agent={NORMAL_UA}")
    args.append("about:blank")

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    info: dict = {"tag": tag, "blocked": None, "ip": None, "ua": None,
                  "h3": 0, "note": ""}
    try:
        import urllib.request

        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(  # noqa: S310
                        f"http://127.0.0.1:{port}/json/version", timeout=2):
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        if not ready:
            info["note"] = "CDP 端口没起来"
            return info

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            info["ua"] = await page.evaluate("() => navigator.userAgent")

            if warmup:
                await page.goto("https://www.google.com/?hl=en",
                                wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(2500)
                for sel in ("#L2AGLb", 'button:has-text("Accept all")'):
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        await page.wait_for_timeout(1500)
                        break

            await page.goto(PROBE_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(5000)
            info["blocked"] = "/sorry/" in page.url
            if info["blocked"]:
                body = await page.inner_text("body")
                m = _IP_RE.search(body)
                info["ip"] = m.group(1) if m else None
            else:
                info["h3"] = (await page.content()).count("<h3")
            await browser.close()
    except Exception as exc:  # noqa: BLE001
        info["note"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
    return info


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    bundled = await bundled_path()
    real = None
    try:
        real = find_chrome()
    except Exception:  # noqa: BLE001
        pass
    print(f"自带 Chromium: {bundled}")
    print(f"真实 Chrome:   {real}\n")

    cases: list[tuple[str, str, dict]] = []
    if bundled:
        cases += [
            ("bundled_headless", bundled, {"headless": True, "override_ua": False}),
            ("bundled_headless_ua", bundled, {"headless": True, "override_ua": True}),
            ("bundled_headful", bundled, {"headless": False, "override_ua": False}),
        ]
    if real:
        cases += [
            ("real_headless_ua", real, {"headless": True, "override_ua": True}),
            ("real_headful", real, {"headless": False, "override_ua": False}),
        ]

    for name, exe, kwargs in cases:
        if args.only and args.only != name:
            continue
        info = await check(exe, name, **kwargs)
        state = ("被拦" if info["blocked"] else
                 "通过" if info["blocked"] is False else "异常")
        print(f"[{name:<20}] {state}  h3={info['h3']:<3} "
              f"ip={info['ip'] or '-':<16} {info['note']}")
        print(f"{'':22} UA={info['ua']}")
        await asyncio.sleep(4)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
