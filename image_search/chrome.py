"""定位并以「普通方式」启动 Chromium 内核浏览器，供 CDP 附加使用。

## 为什么不用 Playwright 直接 launch

Playwright 启动浏览器时会带 ``--enable-automation`` 等标记，还会做一批自动化
相关的注入，Google 的 botguard 能识别出来，判定为机器人后把 ``/search``
重定向到 ``/sorry/index`` 弹 reCAPTCHA。

实测对比（同一个机房 IP、同一个节点）：

* ``playwright.chromium.launch_persistent_context(channel="chrome")`` → /sorry
* 命令行普通启动浏览器 + ``connect_over_cdp`` → 正常返回搜索结果

## 为什么要改 UA

无头模式下浏览器的 UA 会带 ``HeadlessChrome``，这一条就足以被拦。实测：

===================================  ====  ==================================
组合                                 结果  说明
===================================  ====  ==================================
自带 Chromium + 无头（默认 UA）      被拦  UA 含 HeadlessChrome/131
自带 Chromium + 无头 + UA 改成 131   通过  UA 版本与真实版本一致
自带 Chromium + 有头                 通过  有头 UA 本来就不带 Headless
真实 Chrome 152 + 无头 + UA 写 131   被拦  UA 版本和真实版本不符，反而更可疑
真实 Chrome + 有头                   通过
===================================  ====  ==================================

所以规则是：**UA 里不能出现 HeadlessChrome，且版本号必须和浏览器真实版本
一致**。这里的做法是启动后从 CDP 的 ``/json/version`` 读到真实 UA，把
``HeadlessChrome/`` 换成 ``Chrome/`` 再带 ``--user-agent`` 重启一次，
结果缓存在 profile 目录里，之后启动不用再探。

## Docker

AstrBot 在 Linux Docker 里跑时镜像内没有 Chrome，会回退到 Playwright 自带的
Chromium（``playwright install chromium`` 装的那个），实测可用。以 root 运行时
需要 ``--no-sandbox``，这里会自动判断。
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import time

from .exceptions import BrowserNotAvailableError
from .logger import logger

_WINDOWS_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)
_LINUX_CANDIDATES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge", "microsoft-edge-stable",
)
_LINUX_PATHS = (
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/opt/google/chrome/chrome",
)
_MACOS_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

UA_CACHE_NAME = ".detected_user_agent"


def profile_for(base: pathlib.Path, executable: str) -> pathlib.Path:
    """按浏览器隔离 profile 目录。

    同一个 profile 不能在不同版本的浏览器之间共用：用 Chrome 152 写过的
    profile 再交给 Chromium 131 打开，浏览器会直接拒绝启动。所以在
    ``base`` 下按可执行文件分子目录。
    """
    import hashlib

    stem = pathlib.Path(executable).parent.name or "browser"
    digest = hashlib.sha1(executable.encode("utf-8")).hexdigest()[:8]  # noqa: S324
    safe = re.sub(r"[^\w.-]", "_", stem)[:32]
    return base / f"{safe}-{digest}"


def locate_chrome(explicit: str | None = None, bundled: str | None = None,
                  install_dir: pathlib.Path | str | None = None,
                  ) -> tuple[str | None, list[str]]:
    """按顺序找浏览器，返回 ``(路径或 None, 每一步的检查记录)``。

    检查记录用来拼诊断信息 —— 只说「找不到浏览器」没法定位问题，
    得说清楚查了哪些位置、各自是什么情况。

    查找顺序：显式指定 → ``CHROME_PATH`` → 插件自己装的 Chromium →
    系统安装的 Chrome/Chromium/Edge → Playwright 默认位置的 Chromium。
    """
    from .installer import find_chromium_in

    checked: list[str] = []

    if explicit:
        if pathlib.Path(explicit).is_file():
            return explicit, checked
        checked.append(f"配置指定的 chrome_path 不存在: {explicit}")

    env = os.environ.get("CHROME_PATH")
    if env:
        if pathlib.Path(env).is_file():
            return env, checked
        checked.append(f"环境变量 CHROME_PATH 指向的文件不存在: {env}")
    else:
        checked.append("环境变量 CHROME_PATH: 未设置")

    if install_dir:
        local = find_chromium_in(install_dir)
        if local:
            return local, checked
        checked.append(f"插件自装目录里没有 Chromium: {install_dir}")

    system_paths = list(_system_candidates())
    for path in system_paths:
        if pathlib.Path(path).is_file():
            return path, checked
    for name in _LINUX_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found, checked
    checked.append("系统里没有 Chrome/Chromium/Edge（PATH 和常见安装路径都查过了）")

    if bundled:
        if pathlib.Path(bundled).is_file():
            return bundled, checked
        checked.append(f"Playwright 默认位置没有下载过浏览器: {bundled}")
    else:
        checked.append("Playwright 包不可用，拿不到自带 Chromium 的路径")

    return None, checked


def browser_missing_message(checked: list[str],
                            install_dir: pathlib.Path | str | None = None,
                            auto_install_enabled: bool = False) -> str:
    """拼一条能直接照着做的诊断信息。"""
    from .installer import missing_system_libs, playwright_available

    lines = ["找不到可用的浏览器。", "", "已检查："]
    lines.extend(f"  - {item}" for item in checked)

    lines.append("")
    if not playwright_available():
        lines.append("原因：playwright 包没装。请确认插件依赖安装成功："
                     "pip install playwright")
    else:
        lines.append("原因：playwright 包是有的，但浏览器二进制从未下载过。"
                     "AstrBot 只会自动装 pip 依赖，不会执行 playwright install。")

    lines.append("")
    if auto_install_enabled:
        lines.append("插件已开启自动安装，会在后台下载；等几分钟后重试即可。")
        lines.append("若一直失败，可手动执行下面的命令。")
    lines.append("手动安装（在 AstrBot 容器内执行）：")
    target = str(install_dir) if install_dir else "<插件数据目录>/ms-playwright"
    lines.append(f"  PLAYWRIGHT_BROWSERS_PATH={target} \\")
    lines.append("    python -m playwright install --with-deps chromium")
    lines.append("")
    lines.append("注意 --with-deps 不能省：精简镜像缺 Chromium 的系统依赖库，"
                 "只下载浏览器的话会下载成功但启动失败。")

    missing = missing_system_libs()
    if missing:
        lines.append("")
        lines.append(f"当前缺失的依赖库：{', '.join(missing[:8])}"
                     + ("…" if len(missing) > 8 else ""))

    lines.append("")
    lines.append("也可以自己装 Chrome/Chromium 后，用插件配置项 "
                 "chrome_path 或环境变量 CHROME_PATH 指定路径。")
    return "\n".join(lines)


def find_chrome(explicit: str | None = None, bundled: str | None = None,
                install_dir: pathlib.Path | str | None = None) -> str:
    """找一个可用的 Chromium 内核浏览器，找不到就抛带诊断信息的异常。"""
    path, checked = locate_chrome(explicit, bundled, install_dir)
    if path:
        return path
    if explicit and any("chrome_path" in item for item in checked):
        raise BrowserNotAvailableError(f"配置指定的浏览器不存在: {explicit}")
    raise BrowserNotAvailableError(browser_missing_message(checked, install_dir))


def _system_candidates() -> tuple[str, ...]:
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        extra = (str(pathlib.Path(local) / "Google/Chrome/Application/chrome.exe"),
                 ) if local else ()
        return (*_WINDOWS_CANDIDATES, *extra)
    if sys_platform() == "darwin":
        return _MACOS_CANDIDATES
    return _LINUX_PATHS


def sys_platform() -> str:
    import sys

    return sys.platform


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def running_as_root() -> bool:
    """Docker 里常以 root 运行，此时 Chrome 的沙箱起不来，必须 --no-sandbox。"""
    getuid = getattr(os, "geteuid", None)
    return bool(getuid and getuid() == 0)


# ---------------------------------------------------------------------------
# profile 占用清理
#
# Chrome 会在 profile 目录里建 SingletonLock 防止多实例。进程如果没有正常退出
# （容器被杀、Python 进程崩掉、调试时 Ctrl+C），这个锁和浏览器子进程都会残留，
# 下次启动直接失败：
#     Failed to create .../SingletonLock: File exists (17)
#     Failed to create a ProcessSingleton for your profile directory.
# 退出码是 21。所以启动前要主动清理。
# ---------------------------------------------------------------------------
_SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _process_cmdline(pid: int) -> str:
    try:
        raw = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def browsers_using_profile(profile: pathlib.Path) -> list[int]:
    """找出正在使用指定 profile 的浏览器进程 pid（只在 Linux 上有效）。

    通过命令行里的 ``--user-data-dir=<profile>`` 精确匹配。这个 profile 是
    本插件专用的，不会误伤别的程序。
    """
    proc = pathlib.Path("/proc")
    if not proc.is_dir():
        return []
    needle = f"--user-data-dir={profile}"
    pids: list[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        cmdline = _process_cmdline(int(entry.name))
        if needle in cmdline and "chrome" in cmdline.lower():
            pids.append(int(entry.name))
    return pids


def release_profile(profile: pathlib.Path, executable: str | None = None) -> int:
    """清理 profile 的占用：先杀掉遗留浏览器进程，再删掉残留的单例锁。

    Args:
        profile: 浏览器 profile 目录。
        executable: 仅用于日志。

    Returns:
        杀掉的进程数。
    """
    del executable  # 只用于可读性，实际用 profile 精确匹配
    killed = 0
    for pid in browsers_using_profile(profile):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            except OSError:
                break
            time.sleep(0.3)
            try:
                os.kill(pid, 0)
            except OSError:
                break  # 已经没了
        killed += 1
    if killed:
        time.sleep(0.5)
    for name in _SINGLETON_FILES:
        path = profile / name
        try:
            if path.is_symlink() or path.exists():
                path.unlink()
        except OSError:
            pass
    return killed


def normalize_user_agent(user_agent: str) -> str | None:
    """把 UA 里的 ``HeadlessChrome`` 换成 ``Chrome``，版本号保持不变。

    返回 None 表示原本就没问题，不需要覆盖。
    """
    if "HeadlessChrome" not in user_agent:
        return None
    return user_agent.replace("HeadlessChrome/", "Chrome/")


class ChromeProcess:
    """以普通方式启动的浏览器进程，暴露 CDP 端点。"""

    def __init__(self, executable: str, user_data_dir: pathlib.Path,
                 headless: bool = False, proxy: str | None = None,
                 lang: str = "en-US", window_size: tuple[int, int] = (1920, 1080),
                 user_agent: str | None = None,
                 no_sandbox: bool | None = None,
                 extra_args: list[str] | None = None) -> None:
        self.executable = executable
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.proxy = proxy
        self.lang = lang
        self.window_size = window_size
        self.user_agent = user_agent
        self.no_sandbox = running_as_root() if no_sandbox is None else no_sandbox
        self.extra_args = extra_args or []
        self.port = free_port()
        self.process: subprocess.Popen[bytes] | None = None
        self.version_info: dict[str, str] = {}

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def browser_user_agent(self) -> str:
        """浏览器自报的 UA（来自 CDP /json/version）。"""
        return self.version_info.get("User-Agent", "")

    def build_args(self) -> list[str]:
        args = [
            self.executable,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--lang={self.lang}",
            f"--window-size={self.window_size[0]},{self.window_size[1]}",
            # 关掉几个和搜索无关、只会拖慢启动的东西
            "--disable-background-networking",
            "--disable-sync",
            "--disable-features=Translate,OptimizationHints",
        ]
        if self.headless:
            # 新版 headless 用的是同一套渲染栈，比老 headless 隐蔽得多
            args.append("--headless=new")
        if self.user_agent:
            args.append(f"--user-agent={self.user_agent}")
        if self.no_sandbox:
            # 容器内以 root 运行时沙箱起不来
            args.append("--no-sandbox")
            args.append("--disable-setuid-sandbox")
        if sys_platform().startswith("linux"):
            # 容器默认 /dev/shm 只有 64MB，够用但容易崩，改用临时文件
            args.append("--disable-dev-shm-usage")
        if self.proxy:
            args.append(f"--proxy-server={self.proxy}")
        args.extend(self.extra_args)
        args.append("about:blank")
        return args

    def start(self, timeout: float = 40.0) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        # 上一轮没退干净的话，profile 里会留下单例锁和孤儿进程，直接启动会失败
        release_profile(self.user_data_dir, self.executable)
        self._spawn()
        try:
            self.version_info = self._wait_ready(timeout)
        except BrowserNotAvailableError:
            # 退出码 21 = ProcessSingleton 建不起来。再清一次并重试，
            # 覆盖「清理后又有进程抢先占上」这种竞态。
            code = self.process.returncode if self.process else None
            self.process = None
            if code != 21:
                raise
            logger.warning("profile 被占用（退出码 21），清理后重试一次")
            release_profile(self.user_data_dir, self.executable)
            self._spawn()
            self.version_info = self._wait_ready(timeout)

    def _spawn(self) -> None:
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        # 单独一个会话：这样 stop() 能把整棵进程树带走，不留孤儿
        self.process = subprocess.Popen(
            self.build_args(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creation, start_new_session=os.name != "nt")

    def _wait_ready(self, timeout: float) -> dict[str, str]:
        import urllib.error
        import urllib.request

        deadline = time.time() + timeout
        last: Exception | None = None
        while time.time() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise BrowserNotAvailableError(
                    f"浏览器启动后立即退出（code={self.process.returncode}）。"
                    "常见原因：容器里以 root 运行但没加 --no-sandbox、"
                    "缺少 Chromium 的系统依赖库（Linux 上执行 "
                    "`playwright install-deps chromium`），"
                    f"或 profile 目录 {self.user_data_dir} 是更高版本的浏览器写的。")
            try:
                with urllib.request.urlopen(  # noqa: S310
                        f"{self.cdp_url}/json/version", timeout=2) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = exc
                time.sleep(0.3)
        self.stop()
        raise BrowserNotAvailableError(f"等待 CDP 端口超时: {last}")

    def stop(self) -> None:
        """结束浏览器。连整个进程组一起收掉，避免留下孤儿进程。"""
        if self.process is None:
            return
        process, self.process = self.process, None
        pid = process.pid
        with contextlib.suppress(Exception):
            process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                process.wait(timeout=5)
        # Chrome 会拉起一堆子进程，父进程退出不代表它们也退了
        if os.name != "nt":
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
        remaining = browsers_using_profile(self.user_data_dir)
        if remaining:
            logger.debug("仍有 %d 个残留进程占着 profile，强制清理", len(remaining))
            release_profile(self.user_data_dir, self.executable)


def read_cached_user_agent(user_data_dir: pathlib.Path,
                          executable: str) -> str | None:
    """读之前探测到的 UA，避免每次启动都要重启一遍。"""
    path = user_data_dir / UA_CACHE_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("executable") != executable:
        return None
    user_agent = data.get("user_agent")
    return user_agent if isinstance(user_agent, str) and user_agent else None


def write_cached_user_agent(user_data_dir: pathlib.Path, executable: str,
                            user_agent: str) -> None:
    path = user_data_dir / UA_CACHE_NAME
    try:
        user_data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"executable": executable,
                                    "user_agent": user_agent}),
                        encoding="utf-8")
    except OSError:
        pass


_VERSION_RE = re.compile(r"\b(\d+)\.\d+\.\d+\.\d+\b")


def major_version(user_agent: str) -> int | None:
    match = _VERSION_RE.search(user_agent)
    return int(match.group(1)) if match else None
