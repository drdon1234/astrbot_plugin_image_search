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
import dataclasses
import importlib
import json
import os
import pathlib
import re
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


class _Node:
    """对齐 astrbot.api.message_components.Node（合并转发的单个节点）。"""

    def __init__(self, content=None, name: str = "", uin: str = "0") -> None:
        self.content = content or []
        self.name = name
        self.uin = uin

    @property
    def text(self) -> str:
        return "".join(getattr(c, "text", "") for c in self.content)


class _Nodes:
    def __init__(self, nodes=None) -> None:
        self.nodes = nodes or []


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

    def chain_result(self, chain: list):
        return chain

    def get_self_id(self) -> str:
        return "bot-self-id"

    async def send(self, result) -> None:
        self.sent.append(result)

    def stop_event(self) -> None:
        self.stopped = True

    @property
    def forwarded(self) -> list:
        """发出去的合并转发节点（chain_result 返回的是 list）。"""
        for item in self.sent:
            if isinstance(item, list) and item and isinstance(item[0], _Nodes):
                return item[0].nodes
        return []

    @property
    def texts(self) -> list[str]:
        """发出去的普通文本消息。"""
        return [item for item in self.sent if isinstance(item, str)]


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
    module("astrbot.api.message_components", Image=_Image, Reply=_Reply,
           Plain=_Plain, Node=_Node, Nodes=_Nodes)
    module("astrbot.api.star", Context=object, Star=_Star, StarTools=_StarTools,
           register=lambda *a, **k: (lambda cls: cls))
    module("astrbot.core")
    module("astrbot.core.utils")
    # 故意不提供 astrbot.core.utils.session_waiter，
    # 用来验证插件在旧版本 AstrBot 上的降级分支


# ---------------------------------------------------------------------------
# 2. schema / metadata 校验
# ---------------------------------------------------------------------------
def quiet_prepare(plugin):
    """掐掉插件的后台浏览器安装任务，返回同一个实例。

    校验用的是临时数据目录，那里当然没有浏览器，于是 ``_schedule_prepare()``
    的后台任务会真的去执行 ``playwright install``。脚本退出时事件循环已关闭，
    子进程的 transport 才被 GC，于是刷出一串
    ``RuntimeError: Event loop is closed``（只在 Linux 上出现，Windows 的
    Proactor 事件循环不报）。趁 task 还没轮到执行就取消，它不会启动子进程。
    """
    task = getattr(plugin, "_prepare_task", None)
    if task is not None:
        task.cancel()
        plugin._prepare_task = None
    return plugin


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
    plugin = quiet_prepare(
        module.ImageSearchPlugin(context=object(), config=defaults))
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

    # 字段顺序：标题 -> 来源 -> 链接，链接固定在最后一行方便复制。
    # 抬头和第一条之间只有一个换行，所以按行取而不是按空行切块。
    lines = text.splitlines()
    check(lines[1].startswith("1. 标题: BUNNY A GIRL!"),
          f"抬头之后第一行是标题：{lines[1][:26]}")
    check(lines[2] == "来源: らしんばんオンライン", f"第二行是来源：{lines[2]}")
    check(lines[3] == "链接: https://shop.lashinbang.com/products/detail/2273450",
          f"第三行是链接：{lines[3][:30]}")
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

    # ---- AI 正文解析：全部按 DOM 结构判断，不看 AI 具体说了什么 ----
    # 合成一份和真实页面同构的 HTML：main-col 里套一层包装 div，正文段落、
    # 小标题、列表、表格、相关内容网格、追问建议依次是兄弟节点。
    def ai_page(body: str) -> str:
        return ('<div data-subtree="aimc"><div data-container-id="main-col">'
                f'<div data-container-id="7">{body}</div></div></div>')

    # 「相关内容」网格：几十张缩略图 + 外链，正文段落绝不会长成这样
    grid = ('<div>' + "".join(
        f'<a href="https://site{i}.example/p"><img src="t{i}.jpg">'
        f'<span>站点名{i}</span></a>' for i in range(6)) + '</div>')
    # 追问建议：引导语 + 列表，结构和正文里的列表完全一样，只是位置在网格之后
    followup = ('<div data-hveid="f">如果您有兴趣，我可以为您提供：</div>'
                '<ul><li><span>动画的<strong>经典台词</strong></span></li>'
                '<li><span>角色的<strong>身世背景</strong></span></li></ul>'
                '<div data-hveid="g">您想深入了解哪一部分呢？</div>')
    article = (
        '<div data-hveid="a">这张图片是画集<strong>《Bunny A Girl!》</strong>'
        '的封面。'
        '<button aria-label="巴哈姆特（另有 6 个）">巴哈姆特</button>'
        '<img src="favicon.ico"></div>'
        '<div data-hveid="b"><div role="heading">作品详细信息</div></div>'
        '<div data-hveid="c"><ul>'
        '<li><span>角色是<strong>樱岛麻衣</strong></span></li>'
        '<li><span>出自 C95 同人展</span></li></ul></div>'
        '<div data-hveid="d"><table>'
        '<tr><td>角色名称</td><td>樱岛麻衣</td></tr>'
        '<tr><td>作品</td><td>青春猪头少年</td><td>轻小说</td></tr>'
        '</table></div>'
        '<div data-hveid="e">以下是更多相关内容：</div>')

    summary = parser.ai_html_to_text(ai_page(article + grid + followup))
    check("这张图片是画集《Bunny A Girl!》的封面。" in summary,
          "段落里的内联 <strong> 不会把句子切碎")
    check("作品详细信息" in summary, "role=heading 的小标题保留")
    check("• 角色是樱岛麻衣" in summary and "• 出自 C95 同人展" in summary,
          "正文列表保留并加上项目符号")
    # 实测每个 ul 首尾各夹一个空 li，加符号会留下孤零零的「•」
    spaced = parser.ai_html_to_text(ai_page(
        '<div data-hveid="a">正文一段。</div>'
        '<ul><li></li><li><span>有内容的一项</span></li><li></li></ul>'))
    check("• 有内容的一项" in spaced, "有内容的列表项正常加符号")
    check("•" not in [line.strip() for line in spaced.splitlines()],
          "空列表项不会留下孤立的「•」")
    check("角色名称：樱岛麻衣" in summary, "两列表格转成「字段：值」")
    check("作品 | 青春猪头少年 | 轻小说" in summary, "多列表格用竖线分隔")
    check("巴哈姆特" not in summary, "引用来源胶囊（button）被剔除")
    for index in range(6):
        check(f"站点名{index}" not in summary, f"相关内容网格被丢弃：站点名{index}")
    check("如果您有兴趣" not in summary and "经典台词" not in summary
          and "身世背景" not in summary, "网格之后的追问建议一并丢弃")
    check("您想深入了解哪一部分呢" not in summary, "末尾的交互提问一并丢弃")
    check("以下是更多相关内容" not in summary, "引出网格的冒号断尾被削掉")

    # 关键对照：正文列表和追问列表是同构的 <ul>，唯一差别是在网格前还是网格后。
    # 之前用「引导语以冒号结尾」猜，会把「作品详细信息：」这类正文小标题连
    # 带列表一起误删，所以改成只认网格这个位置分界。
    lead_list = parser.ai_html_to_text(ai_page(
        '<div data-hveid="a">画面信息如下：</div>'
        '<div data-hveid="b"><ul><li><span>角色：樱岛麻衣</span></li>'
        '<li><span>场景：海滩</span></li></ul></div>' + grid + followup))
    check("• 角色：樱岛麻衣" in lead_list and "• 场景：海滩" in lead_list,
          "以冒号结尾的引导语 + 列表属于正文时不会被误删")
    check("经典台词" not in lead_list, "同一份 HTML 里网格之后的列表仍被丢弃")

    # 认不出网格时宁可多输出，也不拿措辞去赌 —— 不能截断或丢掉正文
    no_grid = parser.ai_html_to_text(ai_page(article + followup))
    check("这张图片是画集《Bunny A Girl!》的封面。" in no_grid,
          "没有网格时正文完整保留")
    check("经典台词" in no_grid,
          "没有网格时不猜边界，追问建议原样输出（鲁棒性优先）")

    # 不渲染的元素由提取脚本打标记，这里按标记删
    hidden = parser.ai_html_to_text(ai_page(
        '<div data-hveid="a">可见的正文段落。</div>'
        '<h3 data-is-hidden="1">AI 模式针对“1 张图片”的回复</h3>'
        '<div data-is-hidden="1">此对话的副本将包含在内。</div>'
        '<div aria-hidden="true">装饰节点</div>' + grid))
    check("可见的正文段落。" in hidden, "可见正文保留")
    check("AI 模式针对" not in hidden, "隐藏的标题被删")
    check("此对话的副本" not in hidden, "隐藏的分享提示被删")
    check("装饰节点" not in hidden, "aria-hidden 装饰节点被删")

    dup = parser.ai_html_to_text(ai_page(
        '<div data-hveid="a">重复出现的一段描述。</div>'
        '<div data-hveid="b">重复出现的一段描述。</div>'
        '<div data-hveid="c">另一段独立的描述文字。</div>'))
    check(dup.count("重复出现的一段描述。") == 1, "重复段落只保留一次")
    check("另一段独立的描述文字。" in dup, "去重不影响其他段落")

    check(parser.ai_html_to_text("") == "", "空 HTML 返回空串")
    check(parser.ai_html_to_text(ai_page("")) == "", "没有正文时返回空串")

    # AI 拒答当作没有描述，免得输出里只剩一句「抱歉」
    refused = parser.ai_html_to_text(ai_page(
        '<div data-hveid="a">抱歉，我无法提供此图片中相关内容的详细信息或'
        '进行识别。</div>'))
    check(refused == "", "AI 拒答被当成没有描述")
    refused_en = parser.ai_html_to_text(ai_page(
        "<div data-hveid='a'>I can't help with identifying content in "
        "this image.</div>"))
    check(refused_en == "", "英文拒答同样处理")
    long_refusal = parser.ai_html_to_text(ai_page(
        '<div data-hveid="a">' + "这张图片是某部作品的插画。" * 20
        + '另外我无法提供更多细节。</div>'))
    check(long_refusal != "", "长正文里出现类似措辞不会被误判为拒答")
    check(len(parser.ai_html_to_text(
        ai_page('<div data-hveid="a">' + "句子。" * 400 + '</div>'),
        max_chars=200)) <= 210, "超长描述会被截断")

    # ---- 没有网格时，AI 回答原样保留，不做任何剔除 ----
    # 追问和正文在结构上分不开，按措辞或标点去猜都不够稳：网格出现率只有一半，
    # 剩下那半里追问的收尾形态还在「问句」和「引导语 + 列表」之间随机切换。
    # 既然分不可靠就不分，宁可多几行说明文字，也不冒误删正文的风险。
    verbatim = parser.ai_html_to_text(ai_page(
        '<div data-hveid="a">这是正文描述。</div>'
        '<div data-hveid="b">你想进一步了解哪方面的内容呢？</div>'))
    check(verbatim == "这是正文描述。\n你想进一步了解哪方面的内容呢？",
          f"结尾提问原样保留：{verbatim!r}")

    invite = parser.ai_html_to_text(ai_page(
        '<div data-hveid="a">这是正文。</div>'
        '<div data-hveid="b">如果你想了解更多，我可以为你提供：</div>'
        '<ul><li><span>高清壁纸</span></li>'
        '<li><span>剧情简介</span></li></ul>'
        '<div data-hveid="c">请告诉我你接下来想了解的内容。</div>'))
    check("如果你想了解更多" in invite and "• 高清壁纸" in invite
          and "请告诉我" in invite, f"追问建议整块保留：{invite!r}")

    # 但网格必须照旧截断 —— 那是几十个站点名，纯噪声
    grid_still = parser.ai_html_to_text(ai_page(
        '<div data-hveid="a">正文段落。</div>' + grid + followup))
    check("正文段落。" in grid_still, "正文保留")
    for index in range(6):
        check(f"站点名{index}" not in grid_still,
              f"网格仍被丢弃：站点名{index}")
    check("经典台词" not in grid_still, "网格之后的内容仍被丢弃")

    # 网格删掉后悬空的冒号断尾仍要补掉，否则结尾指向一个已不存在的东西
    dangling = parser.ai_html_to_text(ai_page(
        '<div data-hveid="a">她拥有粉色长发。以下是更多相关内容：</div>'
        + grid))
    check(dangling == "她拥有粉色长发。", f"冒号断尾被补掉：{dangling!r}")

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


def _make_plugin(main_module, config):
    """构造插件实例，并掐掉后台的浏览器安装任务（见 :func:`quiet_prepare`）。"""
    return quiet_prepare(
        main_module.ImageSearchPlugin(context=None, config=config))


def _install_session_waiter_stub(picked_image) -> list:
    """注入最小可用的 session_waiter，模拟用户补发一张图片。

    返回一个列表，跑完后里面是补图那条消息对应的事件对象，用来断言
    ``stop_event()`` 作用在它身上、而不是原来的指令事件。
    """
    follow_ups: list = []

    class _Controller:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self, error=None) -> None:
            self.stopped = True

        def keep(self, timeout: float = 0, reset_timeout: bool = False) -> None:
            pass

    def _session_waiter(timeout: int = 30, record_history_chains: bool = False):
        def decorator(func):
            async def wrapper(event, session_filter=None, *args, **kwargs):
                follow_up = _Event([picked_image])
                follow_ups.append(follow_up)
                await func(_Controller(), follow_up)
                return None

            return wrapper

        return decorator

    mod = types.ModuleType("astrbot.core.utils.session_waiter")
    mod.session_waiter = _session_waiter
    mod.SessionController = _Controller
    sys.modules["astrbot.core.utils.session_waiter"] = mod
    return follow_ups


def verify_trigger_pattern() -> None:
    """触发条件：群聊里不 @ 机器人也要能触发，且不能被闲聊误触发。

    对应过的真实故障：群聊中三种写法（``@bot 搜图`` / ``@bot /搜图`` /
    引用图片发 ``搜图``）全部无响应，只有私聊能用。根因是 ``CommandFilter``
    要求 ``is_at_or_wake_command`` 为真、且 ``message_str`` 以指令名开头，
    而群里引用别人的图时既没有 @ 也没有前缀。
    """
    print("16) 触发条件（群聊 / 引用 / @ / 前缀）")
    main_module = importlib.import_module(f"{PACKAGE_NAME}.main")
    trigger = re.compile(main_module.TRIGGER_PATTERN)
    status = re.compile(main_module.STATUS_PATTERN)

    for text in (
        "搜图",                                  # 群里引用图片直接发，没有 @
        " 搜图 ",
        "/搜图",
        "／搜图",                                # 全角斜杠
        "@新一代finisher(2811292152) 搜图",       # @ 机器人后残留的提及文本
        "@新一代finisher(2811292152) /搜图",
        " @新一代 finisher(2811292152)  搜图",    # 昵称里有空格
        "@甲(1) @乙(22) 搜图",                    # 多个 @
        "以图搜图",
        "soutu",
        "SAUCE",                                # 英文别名不区分大小写
    ):
        check(bool(trigger.search(text.strip())), f"能触发：{text!r}")

    for text in (
        "搜图很好用",
        "帮我搜图",
        "搜图状态",                              # 归另一个指令
        "搜索图片",
        "这张图我搜图过了",
        "@新一代finisher(2811292152) 在吗",
        "",
    ):
        check(not trigger.search(text.strip()), f"不误触发：{text!r}")

    check(bool(status.search("搜图状态")), "状态指令可触发")
    check(bool(status.search("@新一代finisher(2811292152) /搜图状态")),
          "状态指令支持 @ 与前缀")
    check(not status.search("搜图"), "搜图不会触发状态指令")


async def verify_message_delivery(defaults: dict) -> None:
    """消息投递：等待补图后必须仍能发出结果，以及各种拆分/合并组合。"""
    print("15) 消息投递（合并转发 / 拆分 / 补图后仍能回复）")
    build_config = plugin_module("image_search.plugin_config").build_config
    formatter = plugin_module("image_search.formatter")
    models = plugin_module("image_search.models")
    main_module = importlib.import_module(f"{PACKAGE_NAME}.main")

    # ---- 回归：补发图片后，原指令事件不能被终止 ----
    # AstrBot 的 pipeline 在每次 yield 之后都检查 is_stopped()，而
    # stop_event() 会把 _force_stopped 永久置位。之前在 _wait_for_image 的
    # finally 里对原事件调了它，导致用户只收到「正在搜索」就没有下文。
    plugin = _make_plugin(main_module, defaults)
    image = _Image(url="https://example.com/pic.png")
    follow_ups = _install_session_waiter_stub(image)
    event = _Event([_Plain("/搜图")])
    picked = await plugin._wait_for_image(event)
    check(picked is image, "等到了用户补发的图片")
    check(event.stopped is False,
          "原指令事件没有被终止（否则后续结果发不出去）")
    check(bool(follow_ups) and follow_ups[0].stopped is True,
          "终止的是补图那条消息，避免它再触发别的插件")

    # 整条指令链路：补图之后仍然要把结果发出来
    async def fake_search(*_args, **_kwargs):
        return models.LensSearchResult(
            exact_matches=[models.ExactMatch(
                url="https://example.com/a", content="标题 A", source="Example")],
            ai_summary="这是一张示例图片。")

    plugin.service.search = fake_search
    plugin.service.browser_ready = lambda: True
    plugin._cooldown.clear()
    _install_session_waiter_stub(image)
    event = _Event([_Plain("/搜图")])
    await plugin.search_image(event)
    sent = [str(m) for m in event.sent]
    check(any("请在" in s and "秒内发送" in s for s in sent), "提示了补发图片")
    check(any(plugin.config.options.working_hint in s for s in sent),
          "发出了搜索中提示")
    check(bool(event.forwarded), "补图路径最终把结果发了出去（合并转发）")
    body = "\n".join(node.text for node in event.forwarded)
    check("这是一张示例图片。" in body and "https://example.com/a" in body,
          "结果内容完整")
    await plugin.terminate()

    # ---- 拆分与合并的各种组合 ----
    result = models.LensSearchResult(
        exact_matches=[
            models.ExactMatch(url="https://example.com/1", content="标题一",
                              source="站点一"),
            models.ExactMatch(url="https://example.com/2", content="标题二",
                              source="站点二"),
        ],
        ai_summary="这是描述。")
    base = build_config(defaults,
                        data_dir=_StarTools.get_data_dir("delivery")).output

    plain = formatter.format_blocks(result, base)
    check(len(plain) == 2, f"默认：描述与结果各一块（实际 {len(plain)}）")
    check(plain[0].startswith("【图片描述】"), "第一块是 AI 描述")
    check("标题一" in plain[1] and "标题二" in plain[1], "结果同在第二块")

    merged = formatter.format_blocks(
        result, dataclasses.replace(base, merge_ai_and_exact=True))
    check(len(merged) == 1, f"开启合并后只有一块（实际 {len(merged)}）")
    check("这是描述。" in merged[0] and "标题一" in merged[0], "两类结果同块")

    split = formatter.format_blocks(
        result, dataclasses.replace(base, link_as_separate_message=True))
    check(split[0].startswith("【图片描述】"), "拆分时描述仍独立成块")
    check("找到以下结果" in split[1], "抬头单独成块")
    # 拆分模式下不带序号和字段名：合并转发里每条已是独立气泡，那些是噪声
    check(split[2] == "标题一（站点一）", f"标题（来源）成一块：{split[2]!r}")
    check(split[3] == "https://example.com/1",
          f"链接裸放，方便长按复制：{split[3]!r}")
    check(split[4] == base.separator, f"结果之间插分隔块：{split[4]!r}")
    check(split[5] == "标题二（站点二）", "第二条紧随分隔块")
    check(split[6] == "https://example.com/2", "第二条的链接也单独成块")
    check(len(split) == 7, f"共 7 块（实际 {len(split)}）")
    check(not any(s.startswith(("1.", "2.", "标题:", "来源:", "链接:"))
                  for s in split), "拆分模式下没有序号，也没有字段名前缀")

    # 关掉站点名时只剩标题，不留空括号
    no_src = formatter.format_blocks(result, dataclasses.replace(
        base, link_as_separate_message=True, show_source=False))
    check(no_src[2] == "标题一", f"不显示来源时只有标题：{no_src[2]!r}")

    # 开了尺寸就一起放进括号
    sized = models.LensSearchResult(exact_matches=[
        models.ExactMatch(url="https://example.com/1", content="标题一",
                          source="站点一", width=1280, height=1796)])
    with_size = formatter.format_blocks(sized, dataclasses.replace(
        base, link_as_separate_message=True, show_size=True))
    check(with_size[1] == "标题一（站点一 · 1280x1796）",
          f"尺寸并入括号：{with_size[1]!r}")

    # 非拆分模式仍然带序号和字段名，那里需要它们来分隔
    check(plain[1].startswith("找到以下结果\n1. 标题: 标题一"),
          f"非拆分模式保留序号与字段名：{plain[1][:32]!r}")

    # 关掉合并转发时，拆分必须失效，否则普通消息会刷屏
    no_forward = formatter.format_blocks(result, dataclasses.replace(
        base, link_as_separate_message=True, use_forward_message=False))
    check(len(no_forward) == 2, f"未开合并转发时不拆分（实际 {len(no_forward)}）")
    check(base.separator not in "".join(no_forward), "也不会插入分隔块")

    # format_result 面向纯文本，强制不拆分
    text = formatter.format_result(result, dataclasses.replace(
        base, link_as_separate_message=True))
    check(base.separator not in text, "format_result 里不出现分隔块")

    # ---- 完全匹配没结果时要明确说出来 ----
    # 只发 AI 描述会让人分不清「这张图没被收录」和「插件没去搜完全匹配」
    ai_only = models.LensSearchResult(ai_summary="这是描述。")
    only_blocks = formatter.format_blocks(ai_only, base)
    check(len(only_blocks) == 2,
          f"描述 + 无结果提示共两块（实际 {len(only_blocks)}）")
    check(only_blocks[1] == base.empty_text,
          f"第二块是无结果提示：{only_blocks[1]!r}")
    check("这是描述。" in only_blocks[0], "AI 描述照常输出")

    merged_only = formatter.format_blocks(
        ai_only, dataclasses.replace(base, merge_ai_and_exact=True))
    check(len(merged_only) == 1 and base.empty_text in merged_only[0],
          "合并模式下提示与描述同块")

    # 用户主动关掉完全匹配时，不该报「没找到」
    off_blocks = formatter.format_blocks(
        ai_only, dataclasses.replace(base, expect_exact_matches=False))
    check(len(off_blocks) == 1 and base.empty_text not in off_blocks[0],
          f"关掉完全匹配时不提示（实际 {off_blocks}）")

    # 两者都空仍然回落到提示，不能发空消息
    nothing = formatter.format_blocks(models.LensSearchResult(), base)
    check(nothing == [base.empty_text], f"两者都空时只有提示：{nothing}")

    # ---- 投递方式 ----
    plugin = _make_plugin(main_module, defaults)
    event = _Event()
    await plugin._send_result(event, result)
    check(len(event.forwarded) == 2, "合并转发的节点数等于块数")
    check(event.forwarded[0].uin == "bot-self-id", "节点带上机器人自身 id")
    check(not event.texts, "合并转发时不额外发普通消息")

    plugin.config.output.use_forward_message = False
    event = _Event()
    await plugin._send_result(event, result)
    check(len(event.texts) == 2, f"关闭后逐块发普通消息（实际 {len(event.texts)}）")
    check(not event.forwarded, "不再走合并转发")

    # 合并转发在别的平台会失败，必须回退而不是丢消息
    plugin.config.output.use_forward_message = True
    event = _Event()

    async def refuse(result_):
        if isinstance(result_, list):
            raise RuntimeError("platform does not support forward")
        event.sent.append(result_)

    event.send = refuse
    await plugin._send_result(event, result)
    check(len(event.texts) == 2, "合并转发失败时回退为普通消息")
    await plugin.terminate()


async def verify_timeout_guard() -> None:
    """底层卡死时，用户必须收到回复，且会话要被重置。

    对应过的真实故障：AstrBot 运行期间 pip 升级了 playwright，旧客户端配新
    driver 让 Playwright 连接层静默挂死，传给它的 timeout 一概无效，
    用户只收到「正在搜索」就没有下文，而且锁被永久持有导致后续请求全部卡住。
    """
    print("12) 卡死兜底与版本一致性")
    main_module = importlib.import_module(f"{PACKAGE_NAME}.main")
    plugin = quiet_prepare(main_module.ImageSearchPlugin(
        context=None, config={"limits": {"request_timeout_seconds": 1}}))
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
    verify_trigger_pattern()
    # 放在最后：它会注入 session_waiter 桩，而第 11 节要验证「没有它」的降级
    await verify_message_delivery(defaults)
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
