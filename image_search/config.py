"""模块配置。"""

from __future__ import annotations

import dataclasses
import pathlib

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Google 的 cookie 同意声明，避免被同意弹窗挡住
SOCS_COOKIE = "CAESHAgBEhJnd3NfMjAyNDA2MTAtMF9SQzIaAmVuIAEaBgiA_LyaBg"

# Lens 结果页的 udm 取值。实测同一个 vsrid 换 udm 就能切换标签页
UDM_ALL = "26"
UDM_VISUAL_MATCHES = "44"
UDM_EXACT_MATCHES = "48"
UDM_AI_MODE = "50"


@dataclasses.dataclass(slots=True)
class SearchConfig:
    """搜索行为配置。

    Attributes:
        headless: 是否无头运行浏览器。调试或需要人工过验证码时设 False。
        use_cdp: 是否用「普通方式启动 Chrome + CDP 附加」。**强烈建议保持
            True** —— Playwright 自己启动浏览器会带自动化标记，Google 的
            botguard 能识别出来并把 ``/search`` 转到 ``/sorry/index``。
        chrome_path: 浏览器可执行文件路径。为空时按「系统 Chrome/Chromium/Edge →
            Playwright 自带 Chromium」的顺序自动查找，也可用环境变量
            ``CHROME_PATH`` 指定。Docker 镜像里一般会落到自带的 Chromium。
        prefer_bundled_chromium: 直接优先用 Playwright 自带的 Chromium，
            不去找系统浏览器。想让行为在各环境间保持一致时可以打开。
        auto_install_browser: 找不到浏览器时是否自动执行
            ``playwright install --with-deps chromium``。AstrBot 装插件只会装
            pip 依赖，不会下载浏览器，所以默认开启。
        install_system_deps: 自动安装时是否带 ``--with-deps`` 装系统依赖库。
            精简镜像缺 ``libnss3`` 等库，不装的话浏览器下载成功也起不来。
            需要 root，非 root 会自动降级。
        install_timeout_seconds: 自动安装的超时秒数。要下载约 170MB 外加 apt 装库。
        browser_install_dir: 浏览器安装目录。留空则放在 ``user_data_dir`` 的
            同级目录下（插件数据目录，是挂载卷，容器重建不会丢）。
        no_sandbox: 是否给浏览器加 ``--no-sandbox``。None 表示自动判断
            （以 root 运行时打开）—— 容器里以 root 跑时不加会起不来。
        user_data_dir: 持久化浏览器 profile 的**父目录**。实际 profile 会按
            浏览器可执行文件分子目录（不同版本共用 profile 会起不来）。
            保留 cookie 可降低触发人机验证的概率。
        proxy: 传给浏览器的代理地址，如 ``http://127.0.0.1:7897``。
            为空则走系统代理 / TUN。
        hl: 结果页语言。
        exact_matches: 是否抓「完全匹配」结果（收录该图的页面列表）。
        ai_mode: 是否抓「AI 模式」的图片描述。和 ``exact_matches`` 相互独立，
            两个都开时只上传一次，然后分别打开两个标签页。
        safe_search: 是否开启 Google 的安全搜索过滤。默认关闭 ——
            实测 ``safe=active`` 会把命中过滤的结果**清空**（不是部分过滤），
            而 Google 的默认值随出口 IP 所在地区变化，部分地区强制开启。
            显式传 ``safe=off`` 才能让行为可预期。
        ai_wait_ms: 等 AI 回答生成完的最长时间（毫秒）。它是流式输出的，
            打开页面时还没写完，实测 11~12 秒收敛。
        timeout_ms: 单步操作超时（毫秒）。
        settle_ms: 结果页渲染后额外等待时间（毫秒），等异步块加载完。
        warmup: 浏览器启动后是否先访问一次 Google 首页拿 cookie。
            每个浏览器生命周期只做一次。
        max_retries: 撞上人机验证时的重试次数。Google 的判定带随机性，
            重新上传再试一次经常就过了。
        retry_delay_s: 重试前的等待秒数。
        max_results: 最多返回多少条 exact matches。
        window_size: 浏览器窗口尺寸。窄窗口会让 Google 把结果标题截得更短，
            所以默认开大一些。
        resolve_concurrency: 还原跳板链接时的并发数。
        complete_titles: 是否尝试抓目标页 ``<title>`` 补全被 Google 截断的标题。
            尽力而为，很多站点有反爬会失败，失败时保留截断标题。
        user_agent: 纯 HTTP 请求（单独上传 / OCR）用的 UA。
        debug_dir: 若设置，会把渲染后的 HTML / 截图落盘。
    """

    headless: bool = True
    use_cdp: bool = True
    chrome_path: str | None = None
    prefer_bundled_chromium: bool = False
    auto_install_browser: bool = True
    install_system_deps: bool = True
    install_timeout_seconds: float = 1800.0
    browser_install_dir: pathlib.Path | None = None
    no_sandbox: bool | None = None
    user_data_dir: pathlib.Path | None = None
    proxy: str | None = None
    hl: str = "en"
    exact_matches: bool = True
    ai_mode: bool = True
    safe_search: bool = False
    ai_wait_ms: int = 30_000
    timeout_ms: int = 60_000
    settle_ms: int = 4_000
    warmup: bool = True
    max_retries: int = 2
    retry_delay_s: float = 3.0
    max_results: int = 20
    window_size: tuple[int, int] = (1920, 1080)
    resolve_concurrency: int = 6
    complete_titles: bool = False
    user_agent: str = DEFAULT_UA
    debug_dir: pathlib.Path | None = None

    def resolved_user_data_dir(self) -> pathlib.Path:
        if self.user_data_dir is not None:
            return pathlib.Path(self.user_data_dir)
        return pathlib.Path.home() / ".cache" / "astrbot_image_search" / "chrome_profile"
