"""缺浏览器时自动补齐。

背景：AstrBot 装插件时只会按 ``requirements.txt`` 装 pip 依赖，不会执行
``playwright install``。所以在官方 Docker 镜像里 Playwright 的 Python 包是有的，
但浏览器二进制从来没下载过 —— ``chromium.executable_path`` 指向一个不存在的文件。
实测还发现两个坑：

* 镜像缺 Chromium 运行所需的系统库（``libnss3`` / ``libatk-1.0`` / ``libcups``），
  光 ``playwright install chromium`` 会下载成功但启动失败，必须 ``--with-deps``。
* ``~/.cache`` 在容器 overlay 文件系统上，不是挂载卷。装到默认位置的话，
  容器一重建浏览器就没了。

所以这里把浏览器装到**插件数据目录**下（那是挂载卷，能持久化），并且只在自己的
子进程里设 ``PLAYWRIGHT_BROWSERS_PATH``，不去动全局环境变量 ——
同一个 AstrBot 进程里可能还有别的插件在用 Playwright，改全局会把它们的浏览器路径带偏。
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

from .logger import logger

#: ``chromium-<rev>/`` 里可执行文件的相对位置。
#: Playwright 1.5x 之后换成了 Chrome for Testing 构建，目录名从
#: ``chrome-linux`` 变成 ``chrome-linux64``，所以两种都要认。
_CHROMIUM_RELATIVE = (
    "chrome-linux64/chrome",       # Chrome for Testing（新）
    "chrome-win64/chrome.exe",
    "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/"
    "Google Chrome for Testing",
    "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/"
    "Google Chrome for Testing",
    "chrome-linux/chrome",         # 旧的 Chromium 归档
    "chrome-win/chrome.exe",
    "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
)
#: 兜底扫描时认的可执行文件名
_CHROMIUM_EXE_NAMES = frozenset(
    {"chrome", "chrome.exe", "Chromium", "Google Chrome for Testing"})
#: Chromium 启动需要、但精简镜像里常缺的系统库。
#: 注意 X11 系列的库名是大写 X（``libXcomposite.so.1``），
#: 写成小写会永远匹配不上、误报成缺失 —— 匹配时统一转小写来兜住这类差异。
_REQUIRED_LIBS = (
    "libnss3.so", "libnssutil3.so", "libatk-1.0.so.0", "libatk-bridge-2.0.so.0",
    "libcups.so.2", "libdrm.so.2", "libgbm.so.1", "libxkbcommon.so.0",
    "libpango-1.0.so.0", "libasound.so.2", "libatspi.so.0",
    "libXcomposite.so.1", "libXdamage.so.1", "libXfixes.so.3", "libXrandr.so.2",
)
_REVISION_RE = re.compile(r"-(\d+)$")

#: 自己兜底装依赖时用的包清单。每组是同一个库在不同发行版上的候选名，
#: 取 apt 里有候选版本的第一个 —— Debian 13 因为 64-bit time_t 迁移
#: 把一批包改成了 ``t64`` 后缀。
#: 刻意不含字体包：Playwright 官方列表里的 ``ttf-unifont`` /
#: ``ttf-ubuntu-font-family`` 是 Ubuntu 专属，Debian 上装不了，而且字体
#: 跟能不能启动无关。
_APT_PACKAGE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("libnss3",),
    ("libnspr4",),
    ("libatk1.0-0t64", "libatk1.0-0"),
    ("libatk-bridge2.0-0t64", "libatk-bridge2.0-0"),
    ("libatspi2.0-0t64", "libatspi2.0-0"),
    ("libcups2t64", "libcups2"),
    ("libdrm2",),
    ("libgbm1",),
    ("libxkbcommon0",),
    ("libpango-1.0-0",),
    ("libcairo2",),
    ("libasound2t64", "libasound2"),
    ("libglib2.0-0t64", "libglib2.0-0"),
    ("libxcomposite1",),
    ("libxdamage1",),
    ("libxfixes3",),
    ("libxrandr2",),
    ("libx11-6",),
    ("libxcb1",),
    ("libxext6",),
    ("libexpat1",),
    ("fonts-liberation",),
)
#: 报错信息里给用户看的手动安装提示（取每组的第一个名字）
_APT_LIB_HINT = tuple(group[0] for group in _APT_PACKAGE_GROUPS)


class InstallState(str, enum.Enum):
    """自动安装的状态。"""

    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


def default_browsers_dir(data_dir: pathlib.Path | None) -> pathlib.Path:
    """浏览器的安装位置。放插件数据目录下，容器重建也不会丢。"""
    base = pathlib.Path(data_dir) if data_dir else (
        pathlib.Path.home() / ".cache" / "astrbot_image_search")
    return base / "ms-playwright"


def _executable_in_revision(revision_dir: pathlib.Path) -> str | None:
    """在单个 ``chromium-<rev>/`` 目录里定位可执行文件。

    先按已知的相对路径直接命中（快），都不中再有界扫描一遍 ——
    这样 Playwright 以后再改目录名也不会直接失效。
    """
    for relative in _CHROMIUM_RELATIVE:
        candidate = revision_dir / relative
        if candidate.is_file():
            return str(candidate)
    for candidate in revision_dir.rglob("*"):
        if candidate.name in _CHROMIUM_EXE_NAMES and candidate.is_file():
            return str(candidate)
    return None


def find_chromium_in(root: pathlib.Path | str | None) -> str | None:
    """在一个 ``PLAYWRIGHT_BROWSERS_PATH`` 风格的目录里找 Chromium。

    有多个版本时取 revision 最大的那个。会跳过 ``chromium_headless_shell-*``
    —— 那个是纯无头精简版，UA 里带 HeadlessChrome 且功能不全，会被 Google 拦。
    """
    if not root:
        return None
    directory = pathlib.Path(root)
    if not directory.is_dir():
        return None

    found: list[tuple[int, str]] = []
    for entry in directory.iterdir():
        # 只认 chromium-<rev>；chromium_headless_shell-<rev> 用下划线，天然被排除
        if not entry.is_dir() or not entry.name.startswith("chromium-"):
            continue
        executable = _executable_in_revision(entry)
        if not executable:
            continue
        match = _REVISION_RE.search(entry.name)
        found.append((int(match.group(1)) if match else 0, executable))
    if not found:
        return None
    found.sort(key=lambda item: item[0], reverse=True)
    return found[0][1]


def missing_system_libs() -> list[str]:
    """列出缺失的 Chromium 依赖库（只在 Linux 上有意义）。"""
    if not sys.platform.startswith("linux"):
        return []
    try:
        output = subprocess.run(  # noqa: S603
            ["ldconfig", "-p"], capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    # 大小写不敏感比对：不同发行版对 X11 库的大小写并不完全一致
    lowered = output.lower()
    return [lib for lib in _REQUIRED_LIBS if lib.lower() not in lowered]


def running_as_root() -> bool:
    getuid = getattr(os, "geteuid", None)
    return bool(getuid and getuid() == 0)


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


class BrowserInstaller:
    """按需下载 Chromium，进程内只会真正执行一次。

    Args:
        install_dir: 浏览器安装目录（会作为子进程的 ``PLAYWRIGHT_BROWSERS_PATH``）。
        with_deps: 是否顺带装系统依赖库。需要 root，非 root 会自动降级。
        timeout: 整个安装过程的超时秒数。下载约 170MB，外加 apt 装库。
    """

    def __init__(self, install_dir: pathlib.Path, with_deps: bool = True,
                 timeout: float = 1800.0) -> None:
        self.install_dir = pathlib.Path(install_dir)
        self.with_deps = with_deps
        self.timeout = timeout
        self.state = InstallState.IDLE
        self.last_error: str = ""
        self.started_at: float = 0.0
        self.finished_at: float = 0.0
        #: 安装完成后仍然缺失的依赖库
        self.missing_libs: list[str] = []
        self._lock = asyncio.Lock()

    # -- 状态 --------------------------------------------------------------
    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.monotonic()
        return end - self.started_at

    def status_text(self) -> str:
        """给用户看的一句话状态。"""
        if self.state is InstallState.RUNNING:
            return f"浏览器正在自动安装中（已用时 {self.elapsed:.0f} 秒，首次约需 2~5 分钟）"
        if self.state is InstallState.FAILED:
            return f"浏览器自动安装失败：{self.last_error}"
        if self.state is InstallState.DONE:
            text = f"浏览器已安装完成（耗时 {self.elapsed:.0f} 秒）"
            if self.missing_libs:
                text += ("\n但仍缺以下系统库，浏览器可能起不来："
                         + ", ".join(self.missing_libs[:6])
                         + ("…" if len(self.missing_libs) > 6 else "")
                         + "\n可在容器内手动执行：apt-get install -y "
                         + " ".join(_APT_LIB_HINT))
            return text
        if self.state is InstallState.SKIPPED:
            return "浏览器自动安装已关闭"
        return "浏览器尚未安装"

    def installed_path(self) -> str | None:
        """已经装好的 Chromium 路径，没有则 None。"""
        return find_chromium_in(self.install_dir)

    # -- 安装 --------------------------------------------------------------
    async def ensure(self) -> str | None:
        """确保浏览器存在，返回可执行文件路径。已在装则等它装完。"""
        existing = self.installed_path()
        if existing:
            self.state = InstallState.DONE
            return existing

        async with self._lock:
            # 可能在排队期间已被别的调用装好
            existing = self.installed_path()
            if existing:
                self.state = InstallState.DONE
                return existing
            if not playwright_available():
                self.state = InstallState.FAILED
                self.last_error = ("playwright 包没装。请检查插件的 requirements.txt "
                                   "是否安装成功（pip install playwright）")
                logger.error("自动安装浏览器失败：%s", self.last_error)
                return None
            return await self._run_install()

    async def _run_install(self) -> str | None:
        """先下载浏览器，再补系统依赖。

        这两步必须分开跑。用 ``playwright install --with-deps chromium`` 一条命令
        的话，装依赖失败会连带浏览器也不下载 —— Playwright 1.49 在 Debian 13
        (trixie) 上就必然踩这个坑：它的依赖列表里有 Ubuntu 专属的字体包
        ``ttf-unifont`` / ``ttf-ubuntu-font-family``，trixie 里没有，
        apt 退出码 100，然后整个安装中止（``Failed to install browsers``）。
        """
        self.state = InstallState.RUNNING
        self.started_at = time.monotonic()
        self.finished_at = 0.0
        self.last_error = ""
        self.missing_libs = []
        self.install_dir.mkdir(parents=True, exist_ok=True)

        # --- 第 1 步：下载浏览器（不带 --with-deps，保证这步不受 apt 影响）---
        logger.info("开始下载 Chromium 到 %s，约 170MB", self.install_dir)
        try:
            code, tail = await self._spawn(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                self._env())
        except asyncio.TimeoutError:
            return self._fail(f"下载超时（超过 {self.timeout:.0f} 秒）")
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"{type(exc).__name__}: {exc}")

        path = self.installed_path()
        if not path:
            return self._fail(
                f"下载浏览器失败（退出码 {code}）"
                + (f"；输出末尾: {tail[-300:]}" if tail else ""))
        logger.info("Chromium 下载完成: %s", path)

        # --- 第 2 步：补系统依赖（失败不致命，浏览器已经在了）---
        if self.with_deps:
            await self._ensure_system_deps()

        self.finished_at = time.monotonic()
        self.missing_libs = missing_system_libs()
        self.state = InstallState.DONE
        if self.missing_libs:
            logger.warning(
                "浏览器已下载，但以下依赖库仍缺失，可能起不来: %s。"
                "可手动执行 apt-get install -y %s",
                ", ".join(self.missing_libs), " ".join(_APT_LIB_HINT))
        else:
            logger.info("浏览器和系统依赖都已就绪（耗时 %.0f 秒）", self.elapsed)
        return path

    def _fail(self, reason: str) -> None:
        self.state = InstallState.FAILED
        self.finished_at = time.monotonic()
        self.last_error = reason
        logger.error("自动安装浏览器失败: %s", reason)
        return None

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # 只在子进程里设，不动全局 —— 同进程可能还有别的插件在用 Playwright
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(self.install_dir)
        # apt 在非交互环境下会卡在配置提示上
        env.setdefault("DEBIAN_FRONTEND", "noninteractive")
        return env

    # -- 系统依赖 ----------------------------------------------------------
    async def _ensure_system_deps(self) -> None:
        """尽力补齐 Chromium 的系统依赖库，失败只告警。"""
        missing = missing_system_libs()
        if not missing:
            logger.debug("系统依赖库齐全，跳过安装")
            return
        logger.info("缺少 Chromium 依赖库: %s", ", ".join(missing))

        if not running_as_root():
            logger.warning("不是 root，装不了系统库。请手动执行 "
                           "`python -m playwright install-deps chromium`")
            return
        if not shutil.which("apt-get"):
            logger.warning("没有 apt-get，无法自动装系统库。"
                           "请按发行版自行安装: %s", ", ".join(missing))
            return

        # 先试 Playwright 官方的 install-deps
        try:
            code, tail = await self._spawn(
                [sys.executable, "-m", "playwright", "install-deps", "chromium"],
                self._env(), timeout=min(self.timeout, 900))
        except Exception as exc:  # noqa: BLE001
            code, tail = -1, f"{type(exc).__name__}: {exc}"
        if code == 0 and not missing_system_libs():
            logger.info("系统依赖库安装完成（playwright install-deps）")
            return

        # 官方列表在某些发行版上装不上（Debian 13 缺 Ubuntu 专属字体包），
        # 退回到自己挑的库清单，只装 apt 里确实存在的那些
        logger.warning("playwright install-deps 未成功（退出码 %s），"
                       "改用自选依赖清单重试。原因通常是官方列表里含本发行版"
                       "没有的包（如 ttf-unifont / ttf-ubuntu-font-family）", code)
        if tail:
            logger.debug("install-deps 输出末尾: %s", tail[-400:])
        await self._apt_install_libs()

    async def _apt_install_libs(self) -> None:
        """自己 apt 装库：先探哪些包名在本发行版存在，再一次性装。

        Debian 13 把一批库改了名（64-bit time_t 迁移，后缀 ``t64``），
        所以每个库都给多个候选名，取 apt 里有候选版本的那个。
        """
        try:
            await self._spawn(["apt-get", "update"], self._env(),
                              timeout=min(self.timeout, 600))
        except Exception as exc:  # noqa: BLE001
            logger.warning("apt-get update 失败，继续尝试安装: %s", exc)

        available = await self._pick_available_packages()
        if not available:
            logger.warning("apt 里没找到任何可用的依赖包，放弃自动安装系统库")
            return
        logger.info("准备安装 %d 个依赖包: %s", len(available),
                    " ".join(available))
        try:
            code, tail = await self._spawn(
                ["apt-get", "install", "-y", "--no-install-recommends",
                 *available],
                self._env(), timeout=min(self.timeout, 900))
        except Exception as exc:  # noqa: BLE001
            logger.warning("apt-get install 出错: %s", exc)
            return
        if code == 0:
            logger.info("系统依赖库安装完成（自选清单）")
        else:
            logger.warning("apt-get install 退出码 %s；输出末尾: %s",
                           code, (tail or "")[-300:])

    async def _pick_available_packages(self) -> list[str]:
        """对每组候选包名，挑出 apt 里真的有的那个。"""
        chosen: list[str] = []
        for group in _APT_PACKAGE_GROUPS:
            for name in group:
                if await self._apt_has_candidate(name):
                    chosen.append(name)
                    break
        return chosen

    async def _apt_has_candidate(self, package: str) -> bool:
        try:
            process = await asyncio.create_subprocess_exec(
                "apt-cache", "policy", package,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=self._env())
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        except Exception:  # noqa: BLE001
            return False
        text = stdout.decode("utf-8", "replace")
        match = re.search(r"Candidate:\s*(\S+)", text)
        return bool(match and match.group(1) != "(none)")

    async def _spawn(self, command: list[str], env: dict[str, str],
                     timeout: float | None = None) -> tuple[int | None, str]:
        """跑一条命令，把关键输出转到日志，返回 (退出码, 输出末尾)。"""
        logger.debug("执行: %s", " ".join(command))
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env)

        lines: list[str] = []

        async def drain() -> None:
            assert process.stdout is not None
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                lines.append(line)
                if len(lines) > 200:
                    del lines[:100]
                # 下载进度刷屏，只挑关键行记日志
                if re.search(r"(Downloading|Installing|error|failed|Error|E:)",
                             line):
                    logger.info("playwright install: %s", line[:200])

        try:
            await asyncio.wait_for(
                asyncio.gather(drain(), process.wait()),
                timeout=timeout if timeout is not None else self.timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            with contextlib.suppress(Exception):
                process.kill()
            raise
        return process.returncode, "\n".join(lines)
