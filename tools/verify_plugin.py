"""离线校验插件入口：用桩模块顶替 astrbot，把 main.py 真正导入并跑一遍。

本机不装 AstrBot 也能验证这些：

* ``_conf_schema.json`` 是合法 JSON，字段类型在 AstrBot 支持的范围内
* schema 默认值经 ``build_config`` 能正确映射成 SearchConfig / OutputOptions
* ``main.py`` 能作为插件包的子模块被导入（相对导入、装饰器都没写错）
* 从消息里取图的三种情况：本条消息带图、引用消息带图、都没有
* 用户冷却逻辑
* 各类异常都被转成可发送的文本，不会把异常抛给 AstrBot

注意：这里一律通过插件包路径（``astrbot_plugin_image_search.image_search.*``）
导入业务模块，和 ``main.py`` 里的相对导入指向同一批模块对象。若改成绝对导入
``image_search.*``，同一个模块会被加载两份，异常类不是同一个对象，
``except RateLimitedError`` 会失配 —— 这个坑正是本脚本第一次跑出来的。

    python tools/verify_plugin.py
    python tools/verify_plugin.py --live    # 额外跑一次真实搜索
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import types

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).parent.parent
PACKAGE_NAME = ROOT.name

ALLOWED_TYPES = {"string", "text", "int", "float", "bool", "list", "object"}
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  [ OK ] {message}")
    else:
        print(f"  [FAIL] {message}")
        failures.append(message)


def plugin_module(name: str):
    """按插件包路径导入业务模块，保证与 main.py 里是同一批模块对象。"""
    return importlib.import_module(f"{PACKAGE_NAME}.{name}")


# ---------------------------------------------------------------------------
# 1. astrbot 桩模块
# ---------------------------------------------------------------------------
class _Image:
    """对齐 astrbot.api.message_components.Image 的关键字段。"""

    def __init__(self, file: str | None = "", url: str = "", path: str = "") -> None:
        self.file = file
        self.url = url
        self.path = path

    async def convert_to_file_path(self) -> str:
        target = self.url or self.file
        if not target:
            raise ValueError("No valid file or URL provided")
        return target


class _Reply:
    def __init__(self, chain=None, message_str: str = "") -> None:
        self.id = "1"
        self.chain = chain or []
        self.message_str = message_str


class _Plain:
    def __init__(self, text: str = "") -> None:
        self.text = text


class _Star:
    def __init__(self, context=None) -> None:
        self.context = context


class _StarTools:
    _dir = pathlib.Path(tempfile.gettempdir()) / "astrbot_plugin_image_search_test"

    @classmethod
    def get_data_dir(cls, plugin_name: str | None = None) -> pathlib.Path:
        path = cls._dir / (plugin_name or "plugin")
        path.mkdir(parents=True, exist_ok=True)
        return path


class _Event:
    """最小可用的 AstrMessageEvent 桩。"""

    def __init__(self, messages=None, sender: str = "u1") -> None:
        self._messages = messages or []
        self._sender = sender
        self.unified_msg_origin = "test:group:1"
        self.sent: list[str] = []
        self.stopped = False

    def get_messages(self):
        return self._messages

    def get_sender_id(self):
        return self._sender

    def plain_result(self, text: str) -> str:
        return text

    async def send(self, result) -> None:
        self.sent.append(result)

    def stop_event(self) -> None:
        self.stopped = True


def _noop_decorator(*_args, **_kwargs):
    def wrap(func):
        return func

    return wrap


def _make_logger():
    import logging

    logging.basicConfig(level=logging.CRITICAL,
                        format="%(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("stub-astrbot")
    logger.setLevel(logging.CRITICAL)
    return logger


def install_astrbot_stubs() -> None:
    """把假的 astrbot 模块塞进 sys.modules，让 main.py 能被导入。"""
    def module(name: str, **attrs) -> types.ModuleType:
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        return mod

    class _PermissionType:
        ADMIN = "admin"
        MEMBER = "member"

    filter_mod = module(
        "astrbot.api.event.filter",
        command=_noop_decorator,
        command_group=_noop_decorator,
        llm_tool=_noop_decorator,
        event_message_type=_noop_decorator,
        regex=_noop_decorator,
        permission_type=_noop_decorator,
        on_astrbot_loaded=_noop_decorator,
        PermissionType=_PermissionType,
    )
    module("astrbot")
    module("astrbot.api", logger=_make_logger())
    module("astrbot.api.event", filter=filter_mod, AstrMessageEvent=_Event)
    module("astrbot.api.message_components", Image=_Image, Reply=_Reply, Plain=_Plain)
    module("astrbot.api.star", Context=object, Star=_Star, StarTools=_StarTools,
           register=lambda *a, **k: (lambda cls: cls))
    module("astrbot.core")
    module("astrbot.core.utils")
    # 故意不提供 astrbot.core.utils.session_waiter，
    # 用来验证插件在旧版本 AstrBot 上的降级分支


# ---------------------------------------------------------------------------
# 2. schema / metadata 校验
# ---------------------------------------------------------------------------
def schema_defaults(schema: dict) -> dict:
    """按 AstrBot 的规则从 schema 推导默认配置字典。"""
    result = {}
    for key, spec in schema.items():
        if spec.get("type") == "object":
            result[key] = schema_defaults(spec.get("items", {}))
        else:
            result[key] = spec.get("default")
    return result


def walk_types(schema: dict, path: str = "") -> list[str]:
    bad = []
    for key, spec in schema.items():
        here = f"{path}.{key}" if path else key
        kind = spec.get("type")
        if kind not in ALLOWED_TYPES:
            bad.append(f"{here}: type={kind!r}")
        if not spec.get("description"):
            bad.append(f"{here}: 缺 description")
        if kind == "object":
            bad.extend(walk_types(spec.get("items", {}), here))
        elif "default" not in spec:
            bad.append(f"{here}: 缺 default")
    return bad


def verify_schema() -> dict:
    print("1) _conf_schema.json")
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    check(True, f"合法 JSON，顶层 {len(schema)} 个分组: {list(schema)}")
    problems = walk_types(schema)
    check(not problems, "字段类型与必填项检查"
          + ("" if not problems else "：" + str(problems)))
    return schema_defaults(schema)


def verify_metadata() -> None:
    print("2) metadata.yaml")
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    fields = {}
    for line in text.splitlines():
        if ":" in line and not line.strip().startswith("#"):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    for key in ("name", "desc", "version", "author", "repo", "astrbot_version"):
        check(bool(fields.get(key)), f"{key} = {fields.get(key)}")
    check(fields.get("name") == PACKAGE_NAME,
          f"name 与目录名一致（{PACKAGE_NAME}）")


# ---------------------------------------------------------------------------
# 3. 配置映射
# ---------------------------------------------------------------------------
def verify_config_mapping(defaults: dict) -> None:
    print("3) 配置映射 build_config")
    build_config = plugin_module("image_search.plugin_config").build_config

    config = build_config(defaults, data_dir=_StarTools.get_data_dir("cfgtest"))
    check(config.search.max_results == 10, f"max_results={config.search.max_results}")
    check(config.search.complete_titles is True,
          f"complete_titles={config.search.complete_titles}")
    check(config.search.timeout_ms == 60_000, f"timeout_ms={config.search.timeout_ms}")
    check(config.search.max_retries == 2, f"max_retries={config.search.max_retries}")
    check(config.search.proxy is None, "空代理串被转成 None")
    check(config.search.use_cdp is True, "use_cdp 保持默认 True（关键项）")
    check("browser_profile" in str(config.search.resolved_user_data_dir()),
          f"profile 落在插件数据目录: {config.search.resolved_user_data_dir()}")
    check(config.output.limit == 10, f"output.limit={config.output.limit}")
    check(config.options.idle_close_minutes == 30,
          f"idle_close_minutes={config.options.idle_close_minutes}")
    check(config.search.auto_install_browser is True,
          f"auto_install_browser={config.search.auto_install_browser}")
    check(config.search.install_system_deps is True,
          f"install_system_deps={config.search.install_system_deps}")
    check(config.search.install_timeout_seconds == 1800,
          f"install_timeout_seconds={config.search.install_timeout_seconds}")
    check("ms-playwright" in str(config.search.browser_install_dir),
          f"浏览器装到插件数据目录: {config.search.browser_install_dir}")

    # 越界值与脏数据要被夹住而不是抛异常
    dirty = build_config({
        "search": {"max_results": 9999, "complete_titles": "false"},
        "browser": {"timeout_seconds": 1, "max_retries": -5, "proxy": "  "},
        "limits": {"user_cooldown_seconds": "abc"},
    })
    check(dirty.search.max_results == 50,
          f"max_results 被夹到 50（{dirty.search.max_results}）")
    check(dirty.search.complete_titles is False, "字符串 'false' 被识别为 False")
    check(dirty.search.timeout_ms == 15_000,
          f"timeout 被夹到 15s（{dirty.search.timeout_ms}）")
    check(dirty.search.max_retries == 0,
          f"max_retries 被夹到 0（{dirty.search.max_retries}）")
    check(dirty.options.user_cooldown_seconds == 15, "非法数字回落到默认值")
    check(build_config(None).search.max_results == 10, "配置为 None 时用默认值")


# ---------------------------------------------------------------------------
# 4. 插件入口
# ---------------------------------------------------------------------------
def verify_plugin_entry(defaults: dict):
    print("4) 导入插件入口 main.py")
    module = plugin_module("main")
    check(hasattr(module, "ImageSearchPlugin"), "ImageSearchPlugin 已定义")
    plugin = module.ImageSearchPlugin(context=object(), config=defaults)
    check(plugin.config.search.max_results == 10, "插件实例读到了配置")
    check(plugin.service.running is False, "浏览器是懒启动，构造时不拉起")
    return plugin


def verify_image_picking(plugin) -> None:
    print("5) 从消息里取图")
    direct = _Event([_Plain("/搜图"), _Image(file="https://example.com/a.png")])
    picked = plugin._pick_image(direct)
    check(picked is not None and picked.file == "https://example.com/a.png",
          "本条消息带图")

    quoted = _Event([_Reply(chain=[_Image(file="https://example.com/b.png")]),
                     _Plain("/搜图")])
    picked = plugin._pick_image(quoted)
    check(picked is not None and picked.file == "https://example.com/b.png",
          "引用的消息带图")

    check(plugin._pick_image(_Event([_Plain("/搜图")])) is None, "没有图片时返回 None")
    check(plugin._pick_image(_Event([])) is None, "空消息返回 None")


def verify_cooldown(plugin) -> None:
    print("6) 用户冷却")
    event = _Event([_Plain("/搜图")], sender="u42")
    check(plugin._check_cooldown(event) == "", "首次调用不冷却")
    again = plugin._check_cooldown(event)
    check(again != "" and "冷却" in again, f"紧接着第二次被拦：{again}")
    other = _Event([_Plain("/搜图")], sender="u43")
    check(plugin._check_cooldown(other) == "", "换个用户不受影响")

    plugin.config.options.user_cooldown_seconds = 0
    check(plugin._check_cooldown(event) == "", "冷却设 0 时不限制")
    plugin.config.options.user_cooldown_seconds = 15


async def verify_error_paths(plugin) -> None:
    print("7) 异常转文本（不能把异常抛给 AstrBot）")
    exceptions = plugin_module("image_search.exceptions")

    async def raiser(exc):
        async def fake_search(*_a, **_k):
            raise exc

        original = plugin.service.search
        plugin.service.search = fake_search
        try:
            return await plugin._run_search("dummy.png")
        finally:
            plugin.service.search = original

    text = await raiser(exceptions.RateLimitedError("captcha"))
    check("人机验证" in text, f"限流 -> {text.splitlines()[0][:30]}")
    text = await raiser(exceptions.BrowserNotAvailableError("no chrome"))
    check("浏览器启动失败" in text, f"浏览器不可用 -> {text.splitlines()[0][:30]}")
    text = await raiser(exceptions.UploadError("boom"))
    check("搜索失败" in text, f"上传失败 -> {text[:30]}")
    text = await raiser(RuntimeError("unexpected"))
    check("详情见 AstrBot 日志" in text, f"未预期异常 -> {text[:30]}")


def verify_formatting(plugin) -> None:
    print("8) 结果格式化")
    formatter = plugin_module("image_search.formatter")
    models = plugin_module("image_search.models")

    result = models.LensSearchResult(
        exact_matches=[
            models.ExactMatch(
                url="https://shop.lashinbang.com/products/detail/2273450",
                content="BUNNY A GIRL! 【青春ブタ野郎 シリーズ】[溝口ケージ][NtyPe]",
                source="らしんばんオンライン", width=1280, height=1796),
            models.ExactMatch(
                url="https://www.suruga-ya.jp/product/detail/ZHORO56726",
                content="中古 BUNNY A GIRL!", source="駿河屋"),
        ],
        ocr_text="Bunny A Girl!",
    )
    text = formatter.format_result(result, plugin.config.output)
    check("链接: https://shop.lashinbang.com/products/detail/2273450" in text,
          "输出含链接行")
    check("标题: BUNNY A GIRL!" in text, "输出含标题行")
    check("来源: らしんばんオンライン" in text, "默认输出站点名")
    check(text.startswith("找到以下结果"), "抬头为「找到以下结果」")
    check("尺寸:" not in text, "默认不输出尺寸")
    print("     ---- 实际输出 ----")
    for line in text.splitlines():
        print(f"     {line}")

    empty = formatter.format_result(models.LensSearchResult(), plugin.config.output)
    check(empty == plugin.config.output.empty_text, f"空结果 -> {empty}")


def verify_browser_discovery() -> None:
    print("9) 浏览器查找与自动安装")
    chrome = plugin_module("image_search.chrome")
    installer = plugin_module("image_search.installer")

    # 在临时目录里造出 Playwright 的目录结构，验证能被找到
    fake = pathlib.Path(tempfile.mkdtemp(prefix="ms-playwright-test-"))
    layouts = {
        "linux": "chromium-1148/chrome-linux/chrome",
        "windows": "chromium-1148/chrome-win/chrome.exe",
    }
    check(installer.find_chromium_in(fake) is None, "空目录里找不到 Chromium")
    for name, rel in layouts.items():
        target = fake / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("stub", encoding="utf-8")
        found = installer.find_chromium_in(fake)
        check(found is not None and rel.replace("/", os.sep) in found,
              f"能识别 {name} 布局: {rel}")
        target.unlink()

    # 多版本时取 revision 最大的
    for rev in (1100, 1148, 1200):
        target = fake / f"chromium-{rev}/chrome-linux/chrome"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("stub", encoding="utf-8")
    found = installer.find_chromium_in(fake) or ""
    check("chromium-1200" in found, f"多版本时取最新: {found}")

    # headless_shell 不能被误选（UA 带 HeadlessChrome，会被 Google 拦）
    shell = fake / "chromium_headless_shell-9999/chrome-linux/chrome"
    shell.parent.mkdir(parents=True, exist_ok=True)
    shell.write_text("stub", encoding="utf-8")
    found = installer.find_chromium_in(fake) or ""
    check("headless_shell" not in found, f"跳过 headless_shell: {found}")
    shutil.rmtree(fake, ignore_errors=True)

    # 依赖库检测：X11 系列库名是大写 X，小写写法会永远误报缺失
    saved_run = installer.subprocess.run
    fake_ldconfig = ("\tlibXcomposite.so.1 (libc6,x86-64) => /lib/libXcomposite.so.1\n"
                     "\tlibXdamage.so.1 (libc6,x86-64) => /lib/libXdamage.so.1\n"
                     "\tlibXfixes.so.3 (libc6,x86-64) => /lib/libXfixes.so.3\n"
                     "\tlibXrandr.so.2 (libc6,x86-64) => /lib/libXrandr.so.2\n"
                     "\tlibnss3.so (libc6,x86-64) => /lib/libnss3.so\n")

    class _Result:
        stdout = fake_ldconfig

    try:
        installer.subprocess.run = lambda *_a, **_k: _Result()
        installer.sys.platform = "linux"
        missing = installer.missing_system_libs()
    finally:
        installer.subprocess.run = saved_run
        installer.sys.platform = sys.platform

    for lib in ("libXcomposite.so.1", "libXdamage.so.1", "libXfixes.so.3",
                "libXrandr.so.2", "libnss3.so"):
        check(lib not in missing, f"{lib} 已装则不误报缺失")
    check("libcups.so.2" in missing, "真缺的库仍能被报出来（libcups.so.2）")

    # locate_chrome 找不到时要留下每一步的检查记录。
    # 本机可能装了真 Chrome，所以把系统查找和 CHROME_PATH 都屏蔽掉，
    # 让这段断言不依赖运行环境。
    empty = pathlib.Path(tempfile.mkdtemp(prefix="empty-browsers-"))
    saved_candidates = chrome._system_candidates
    saved_which = chrome.shutil.which
    saved_env = os.environ.pop("CHROME_PATH", None)
    try:
        chrome._system_candidates = lambda: ()
        chrome.shutil.which = lambda _name: None
        path, checked = chrome.locate_chrome(
            explicit="/definitely/not/here/chrome",
            bundled="/also/not/here/chrome",
            install_dir=empty)
    finally:
        chrome._system_candidates = saved_candidates
        chrome.shutil.which = saved_which
        if saved_env is not None:
            os.environ["CHROME_PATH"] = saved_env

    check(path is None, f"全都找不到时返回 None（实际 {path}）")
    check(len(checked) >= 4, f"记录了 {len(checked)} 条检查过程")
    message = chrome.browser_missing_message(checked, empty,
                                            auto_install_enabled=True)
    for needle in ("已检查", "--with-deps", "PLAYWRIGHT_BROWSERS_PATH",
                   "CHROME_PATH", "自动安装"):
        check(needle in message, f"诊断信息包含 {needle!r}")
    shutil.rmtree(empty, ignore_errors=True)
    print("     ---- 诊断信息实样 ----")
    for line in message.splitlines():
        print(f"     {line}")


def verify_install_paths(plugin) -> None:
    print("10) 安装目录与状态查询")
    check(plugin.service.browser_ready() in (True, False),
          f"browser_ready() 可调用（当前 {plugin.service.browser_ready()}）")
    status = plugin.service.install_status()
    check(bool(status), f"install_status() -> {status}")
    session = plugin.service._probe()
    check("ms-playwright" in str(session.browsers_dir),
          f"安装目录: {session.browsers_dir}")
    check(session.installer.install_dir == session.browsers_dir,
          "installer 用的是同一个目录")


async def verify_wait_fallback(plugin) -> None:
    print("11) 旧版本 AstrBot 没有 session_waiter 时的降级")
    event = _Event([_Plain("/搜图")])
    picked = await plugin._wait_for_image(event)
    check(picked is None, "返回 None")
    check(any("带上图片" in str(m) for m in event.sent),
          f"发了引导提示：{event.sent}")

    plugin.config.options.wait_image_seconds = 0
    event2 = _Event([_Plain("/搜图")])
    check(await plugin._wait_for_image(event2) is None, "等待时间设 0 时直接提示")
    plugin.config.options.wait_image_seconds = 60


def verify_result_modes(defaults: dict) -> None:
    """两种结果模式是正交的：各自独立开关，输出互不依赖。"""
    print("14) 结果模式（AI 描述 / 完全匹配）")
    build_config = plugin_module("image_search.plugin_config").build_config
    parser = plugin_module("image_search.parser")
    formatter = plugin_module("image_search.formatter")
    models = plugin_module("image_search.models")
    uploader = plugin_module("image_search.uploader")

    config = build_config(defaults, data_dir=_StarTools.get_data_dir("modes"))
    check(config.search.exact_matches is True, "默认开启完全匹配")
    check(config.search.ai_mode is True, "默认开启 AI 描述")
    check(config.search.safe_search is False, "默认关闭安全搜索过滤")

    location = "https://www.google.com/search?vsrid=ABC&udm=26"
    check("udm=48" in uploader.to_exact_matches_url(location, "en"),
          "完全匹配页 udm=48")
    check("udm=50" in uploader.to_ai_mode_url(location, "en"),
          "AI 模式页 udm=50")
    check("safe=off" in uploader.to_ai_mode_url(location, "en"),
          "默认显式带 safe=off")
    check("safe=active" in uploader.to_ai_mode_url(location, "en", True),
          "开启过滤时带 safe=active")

    # AI 正文清理：剔除独立链接行、滤掉框架文案、在引导语处截断
    payload = {
        "started": True,
        "blocks": [
            "AI 模式",
            "若要访问历史记录和获享其他好处，请登录您的账号",
            "这张图片是轻小说《青春猪头少年系列》的同人画集封面。",
            "图中描绘的角色是女主角樱岛麻衣。",
            "Character：Mai Sakurajima",
            "Origin：C95 Comiket Artbook BUNNY A GIRL!",
            "rascaldoesnotdream.com",
            "推荐原作小说的阅读顺序与各卷标题",
            "如果你对这个系列感兴趣，我们可以聊聊：",
            "系列的最新剧情发展",
        ],
        "drop": ["rascaldoesnotdream.com", "推荐原作小说的阅读顺序与各卷标题"],
        "charCount": 160,
    }
    summary = parser.clean_ai_summary(payload)
    check("同人画集封面" in summary and "樱岛麻衣" in summary, "保留了正文段落")
    check("Character：Mai Sakurajima" in summary
          and "Origin：C95" in summary, "保留了表格行")
    check("登录您的账号" not in summary, "滤掉了登录提示")
    check("AI 模式" not in summary, "滤掉了标签栏文案")
    check("rascaldoesnotdream.com" not in summary, "剔除了来源标记（独立链接行）")
    check("阅读顺序与各卷标题" not in summary, "剔除了追问建议（独立链接行）")
    check("我们可以聊聊" not in summary and "最新剧情发展" not in summary,
          "在引导语处截断")

    # 引导语的几种真实说法，含敬语
    for lead in ("如果您对该作品感兴趣，我可以为您提供更多相关信息：",
                 "如需了解更多相关精彩内容，您可以浏览以下精选剧照与插画：",
                 "如果你对这个系列感兴趣，我们可以聊聊：",
                 "If you're interested, I can share more:",
                 "Explore similar official artwork:"):
        cut = parser.clean_ai_summary({"blocks": [
            "这张图片是某作品的官方插画，画面里有一名角色。",
            lead,
            "建议条目一", "建议条目二",
        ], "drop": [], "charCount": 120})
        check("官方插画" in cut and "建议条目" not in cut,
              f"引导语处截断：{lead[:20]}")

    dup = parser.clean_ai_summary({"blocks": ["重复出现的一段描述。",
                                             "重复出现的一段描述。",
                                             "另一段独立的描述文字。"],
                                   "drop": [], "charCount": 60})
    check(dup.count("重复出现的一段描述。") == 1, "重复块只保留一次")
    check("另一段独立的描述文字。" in dup, "去重不影响其他段落")

    # 卡片区一旦出现就收尾
    for card_line in ("wall.alphacoders.com",
                      "2023年7月17日 — Mai Sakurajima in Skirt",
                      "17 July 2023 — Some English summary",
                      "情報】溝口ケージ老師 C95 全彩本封面公開 ... · 8 years ago",
                      "青春猪头少年】OST 1小时循环_哔哩哔哩 · 2 months ago",
                      "全部显示"):
        cut = parser.clean_ai_summary({"blocks": [
            "这是一段正常的图片描述文字，说明画面内容。",
            card_line,
            "这一行在卡片之后，不应出现",
        ], "drop": [], "charCount": 200})
        check("正常的图片描述文字" in cut and "不应出现" not in cut,
              f"遇到卡片行即收尾：{card_line[:26]}")
    check(parser.clean_ai_summary({"blocks": ["作品编号：ABCD-123"],
                                   "drop": [], "charCount": 20})
          == "作品编号：ABCD-123", "含点号的正常字段不被当成域名")

    # 引用标记：整行等于链接文字才丢，内联链接不受影响
    inline = parser.clean_ai_summary({"blocks": [
        "这张图片出自《青春猪头少年系列》，是官方插画。",
        "巴哈姆特",
        "DARLING in the FRANXX Wiki",
    ], "drop": ["青春猪头少年系列", "巴哈姆特", "DARLING in the FRANXX Wiki"],
        "charCount": 120})
    check("《青春猪头少年系列》，是官方插画" in inline,
          "句中内联的链接不影响整行")
    check("巴哈姆特" not in inline.splitlines()
          and "DARLING in the FRANXX Wiki" not in inline.splitlines(),
          "独占一行的引用标记被剔除")

    # 尾部裁剪：末尾的来源站点名要削掉，正文和「字段：值」要留住
    tailed = parser.clean_ai_summary({"blocks": [
        "这张图片是某作品的官方插画。",
        "画面内容： 角色站在海滩上。",
        "手机新浪网",
        "哈啦區- 巴哈姆特",
        "Pinterest",
    ], "drop": [], "charCount": 120}).splitlines()
    check(tailed[-1].startswith("画面内容"), "末尾停在最后一条正文上")
    for noise in ("手机新浪网", "哈啦區- 巴哈姆特", "Pinterest"):
        check(noise not in tailed, f"尾部噪声被削掉：{noise}")
    kept_tail = parser.clean_ai_summary({"blocks": [
        "这张图片是某作品的插画。", "代码： Code:002",
    ], "drop": [], "charCount": 60}).splitlines()
    check(kept_tail[-1] == "代码： Code:002", "「字段：值」不会被尾部裁剪误删")
    check(parser.clean_ai_summary({"started": False, "blocks": [],
                                   "charCount": 0}) == "",
          "没有内容时返回空串")

    # 页面框架文案（拿不到 aimfl 锚点时的兜底过滤）
    framed = parser.clean_ai_summary({"blocks": [
        "跳到主要内容 无障碍功能帮助", "管理 AI 模式共享的公开链接",
        "AI 模式历史记录", "您已退出账号", "AI 模式对话", "您发送了：1 张图片",
        "这张图片是某部动画的宣传插画，画面里有一名角色。",
        "See less", "分享公开链接", "此公开链接在 7 天内有效，用于分享消息串。",
        "Facebook",
    ], "charCount": 200})
    check("宣传插画" in framed, "框架文案里仍能取出正文")
    for noise in ("跳到主要内容", "您已退出账号", "您发送了", "See less",
                  "分享公开链接", "Facebook"):
        check(noise not in framed, f"滤掉了「{noise}」")

    # AI 拒答当作没有描述
    refused = parser.clean_ai_summary({"blocks": [
        "抱歉，我无法提供此图片中相关内容的详细信息或进行识别。",
    ], "charCount": 30})
    check(refused == "", "AI 拒答被当成没有描述")
    refused_en = parser.clean_ai_summary({"blocks": [
        "I can't help with identifying content in this image.",
    ], "charCount": 50})
    check(refused_en == "", "英文拒答同样处理")
    long_refusal = parser.clean_ai_summary({"blocks": [
        "这张图片是某部作品的插画。" * 20 + "另外我无法提供更多细节。",
    ], "charCount": 400})
    check(long_refusal != "", "长正文里出现类似措辞不会被误判为拒答")
    long_payload = {"blocks": ["句子。" * 400], "charCount": 1200}
    check(len(parser.clean_ai_summary(long_payload, max_chars=200)) <= 210,
          "超长描述会被截断")

    # 输出：三种组合都要正常
    match = models.ExactMatch(url="https://example.com/a", content="标题 A",
                              source="Example")
    both = models.LensSearchResult(exact_matches=[match],
                                   ai_summary="这是一张示例图片。")
    text = formatter.format_result(both, config.output)
    check("【图片描述】" in text and "这是一张示例图片。" in text
          and "链接: https://example.com/a" in text, "两种模式同时输出")
    check(text.index("这是一张示例图片。") < text.index("链接:"),
          "AI 描述排在完全匹配之前")

    ai_only = models.LensSearchResult(ai_summary="只有描述。")
    text_ai = formatter.format_result(ai_only, config.output)
    check("只有描述。" in text_ai and "链接:" not in text_ai,
          "只有 AI 描述时不输出空列表")
    check(bool(ai_only) is True, "只有 AI 描述也算有结果")

    exact_only = models.LensSearchResult(exact_matches=[match])
    text_exact = formatter.format_result(exact_only, config.output)
    check("链接: https://example.com/a" in text_exact
          and "【图片描述】" not in text_exact, "只有完全匹配时不输出空抬头")

    empty = formatter.format_result(models.LensSearchResult(), config.output)
    check(empty == config.output.empty_text, f"两者都空 -> {empty}")

    off = build_config({"search": {"exact_matches": False, "ai_mode": False}},
                       data_dir=_StarTools.get_data_dir("modes"))
    check(off.search.exact_matches is False and off.search.ai_mode is False,
          "两个模式都可以关掉（运行时会报错提示）")


async def verify_timeout_guard() -> None:
    """底层卡死时，用户必须收到回复，且会话要被重置。

    对应过的真实故障：AstrBot 运行期间 pip 升级了 playwright，旧客户端配新
    driver 让 Playwright 连接层静默挂死，传给它的 timeout 一概无效，
    用户只收到「正在搜索」就没有下文，而且锁被永久持有导致后续请求全部卡住。
    """
    print("12) 卡死兜底与版本一致性")
    main_module = importlib.import_module(f"{PACKAGE_NAME}.main")
    plugin = main_module.ImageSearchPlugin(
        context=None, config={"limits": {"request_timeout_seconds": 1}})
    check(plugin.config.options.request_timeout_seconds == 1,
          "总超时配置生效（1 秒）")

    hung = asyncio.Event()          # 永不 set，模拟永久挂起
    closed = asyncio.Event()
    saved_close = plugin.service.close

    async def never_returns(*_args, **_kwargs):
        await hung.wait()

    async def fake_close(*_args, **_kwargs):
        closed.set()

    plugin.service.search = never_returns
    plugin.service.close = fake_close
    loop = asyncio.get_running_loop()
    started = loop.time()
    text = await plugin._run_search("whatever.png")
    elapsed = loop.time() - started

    check(elapsed < 6, f"没有无限等待，{elapsed:.1f} 秒后返回")
    check("超时" in text, f"回复了超时提示：{text.splitlines()[0]}")
    check(closed.is_set(), "超时后强制关闭了浏览器会话，下次搜索可重新开始")

    plugin.service.close = saved_close
    await plugin.terminate()

    browser = plugin_module("image_search.browser")
    mismatch = browser.playwright_version_mismatch()
    check(mismatch is None,
          f"当前环境 playwright 版本一致（实际 {mismatch}）")
    hint = browser._VERSION_MISMATCH_HINT.format(loaded="1.49.1",
                                                 on_disk="1.62.0")
    check("重启 AstrBot" in hint, "版本不一致时的提示给出了解决办法")


async def verify_live(plugin) -> None:
    print("13) 真实搜索（--live）")
    text = await plugin._run_search(str(ROOT / "test_imgs" / "test.png"))
    check("链接: http" in text, "拿到了真实结果")
    print("     ---- 实际输出 ----")
    for line in text.splitlines():
        print(f"     {line}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="额外跑一次真实搜索")
    args = ap.parse_args()

    # 只把仓库的父目录加进 sys.path，这样业务模块只会以插件包子模块的形式被加载一次
    if str(ROOT.parent) not in sys.path:
        sys.path.insert(0, str(ROOT.parent))
    install_astrbot_stubs()

    defaults = verify_schema()
    verify_metadata()
    verify_config_mapping(defaults)
    plugin = verify_plugin_entry(defaults)
    verify_image_picking(plugin)
    verify_cooldown(plugin)
    await verify_error_paths(plugin)
    verify_formatting(plugin)
    verify_browser_discovery()
    verify_install_paths(plugin)
    await verify_wait_fallback(plugin)
    await verify_timeout_guard()
    verify_result_modes(defaults)
    if args.live:
        await verify_live(plugin)

    await plugin.terminate()
    check(plugin.service.running is False, "terminate 后浏览器已关闭")

    print()
    if failures:
        print(f"=== 校验失败 {len(failures)} 项 ===")
        for item in failures:
            print(" -", item)
        return 1
    print("=== 插件入口校验全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
