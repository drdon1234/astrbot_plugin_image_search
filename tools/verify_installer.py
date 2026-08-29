"""实测浏览器自动安装：真的下载一次，再用装好的浏览器搜一次图。

会往临时目录下载约 170MB，跑完自动清理（加 --keep 可保留）。
这是对 Docker 场景的等效验证 —— 那边的表现差异只在 ``--with-deps``
（需要 root + apt，Windows 上会自动跳过并给出警告）。

    python tools/verify_installer.py
    python tools/verify_installer.py --keep     # 保留下载结果
    python tools/verify_installer.py --no-search  # 只测安装，不搜图
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
import shutil
import sys
import tempfile
import time

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from image_search import SearchConfig  # noqa: E402
from image_search.browser import BrowserSession  # noqa: E402
from image_search.installer import (  # noqa: E402
    BrowserInstaller,
    InstallState,
    find_chromium_in,
    missing_system_libs,
    running_as_root,
)
from image_search.searcher import GoogleLensSearcher  # noqa: E402

OK, BAD, WARN = "[ OK ]", "[FAIL]", "[WARN]"
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {OK if condition else BAD} {message}")
    if not condition:
        failures.append(message)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留下载的浏览器")
    ap.add_argument("--no-search", action="store_true", help="只测安装，不搜图")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    workdir = pathlib.Path(tempfile.mkdtemp(prefix="astrbot-install-test-"))
    install_dir = workdir / "ms-playwright"
    print(f"测试目录: {workdir}")
    print(f"以 root 运行: {running_as_root()}  "
          f"（False 时会跳过 --with-deps，Docker 里是 root 所以会带上）")
    missing = missing_system_libs()
    print(f"缺失的 Chromium 依赖库: {missing or '无（或非 Linux）'}\n")

    try:
        print("1) 安装前状态")
        installer = BrowserInstaller(install_dir, with_deps=True, timeout=1800)
        check(installer.installed_path() is None, "安装目录里还没有浏览器")
        check(installer.state is InstallState.IDLE,
              f"初始状态 {installer.state.value}")
        print(f"        状态文案: {installer.status_text()}")

        print("\n2) 执行安装（要下载约 170MB，请耐心等）")
        started = time.monotonic()
        path = await installer.ensure()
        elapsed = time.monotonic() - started
        check(path is not None, f"安装返回可执行文件路径（耗时 {elapsed:.0f} 秒）")
        check(installer.state is InstallState.DONE,
              f"状态变为 done（实际 {installer.state.value}）")
        if not path:
            print(f"        失败原因: {installer.last_error}")
            return 1
        print(f"        路径: {path}")
        check(pathlib.Path(path).is_file(), "文件真实存在")
        check("headless_shell" not in path, "装的是完整 Chromium，不是 headless_shell")
        check(find_chromium_in(install_dir) == path, "独立的查找函数能找到同一个")
        print(f"        状态文案: {installer.status_text()}")

        print("\n3) 幂等性：再调一次不应重新下载")
        started = time.monotonic()
        again = await installer.ensure()
        check(again == path, "返回同一个路径")
        check(time.monotonic() - started < 5, "秒回，没有重新下载")

        print("\n4) BrowserSession 能发现自动安装的浏览器")
        config = SearchConfig(
            browser_install_dir=install_dir,
            user_data_dir=workdir / "profile",
            auto_install_browser=True,
        )
        session = BrowserSession(config)
        check(session.browsers_dir == install_dir,
              f"browsers_dir 指向安装目录: {session.browsers_dir}")
        check(session.browser_ready() is True, "browser_ready() 为 True")

        if args.no_search:
            return 0 if not failures else 1

        print("\n5) 用装好的浏览器实际搜一次图")
        searcher = GoogleLensSearcher(config)
        try:
            await searcher.start()
            print(f"        实际使用: {searcher._browser.executable}")
            print(f"        生效 UA:  {searcher._browser.user_agent}")
            check(str(install_dir) in str(searcher._browser.executable),
                  "用的就是自动安装的那个浏览器")
            result = await searcher.search(ROOT / "test_imgs" / "test.png")
            check(bool(result.exact_matches),
                  f"搜到 {len(result.exact_matches)} 条结果")
            for match in result.exact_matches[:3]:
                print(f"        url: {match.url[:90]}")
                print(f"        content: {match.content[:60]}")
        finally:
            await searcher.close()
        return 0 if not failures else 1
    finally:
        if args.keep:
            print(f"\n已保留: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)
            print(f"\n已清理: {workdir}")
        if failures:
            print(f"=== 失败 {len(failures)} 项 ===")
            for item in failures:
                print(" -", item)
        else:
            print("=== 自动安装校验全部通过 ===")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
