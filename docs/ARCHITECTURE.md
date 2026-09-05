# 架构文档

本文件按当前项目的真实实现描述插件边界、模块职责与主流程。
各项技术选择的依据、被否决的方案与实测数据见 [DESIGN_NOTES.md](DESIGN_NOTES.md)。

## 一、整体框架

### 1.1 系统概述

本项目是 AstrBot 图片反向检索插件。插件接收用户消息中的图片，通过 Google Lens
的 Web 界面提交检索，返回 AI 图片描述，并解析 **Exact matches（完全匹配）**
结果页，给出收录该图片的页面地址与标题。

实现上分为两层：

- `image_search/`：业务层不要求 AstrBot；日志适配器会在 AstrBot 环境中复用其
  logger，独立运行时回退标准库。
- `main.py`：AstrBot 适配层，负责指令注册、消息取图、冷却控制、异常转文案与
  生命周期管理。

当前正式检索链路经由真实浏览器完成。带有效浏览器会话 cookie 的纯 HTTP 路径已被
实验验证，但失效回退、凭据存储和代理切换尚未产品化，因此不属于当前实现。
详细论证见 DESIGN_NOTES 第 3、4 节。

### 1.2 核心模块结构

```text
astrbot_plugin_image_search/
├── main.py                      # AstrBot 插件入口与生命周期
├── metadata.yaml                # 插件元信息
├── _conf_schema.json            # AstrBot 配置 schema
├── requirements.txt             # httpx / playwright / pillow / beautifulsoup4
├── docs/
│   ├── ARCHITECTURE.md          # 当前架构文档
│   └── DESIGN_NOTES.md          # 技术选择依据与排错备忘
├── tools/                       # 管理员排查脚本
└── image_search/
    ├── config.py                # SearchConfig：搜索与浏览器行为配置
    ├── plugin_config.py         # AstrBot 配置字典 → 三份配置对象
    ├── models.py                # ExactMatch / LensSearchResult
    ├── exceptions.py            # 异常体系
    ├── logger.py                # logger 适配与第三方库日志降噪
    ├── http_client.py           # HTTP 重定向、总时限与流式读取上限
    ├── loader.py                # 输入归一化、动图提取首帧
    ├── uploader.py              # 上传参数构造、udm 改写
    ├── session.py               # LensSession：HTTP 上传与 OCR 共享会话
    ├── metadata.py              # qfmetadata 的 JSPB 解析
    ├── chrome.py                # 浏览器定位、进程启动、UA 规范化、profile 管理
    ├── installer.py             # 浏览器与系统依赖自动安装
    ├── browser.py               # BrowserSession：页面级操作与链接还原
    ├── parser.py                # 结果页提取脚本与行结构解析
    ├── titles.py                # 可选的标题补全
    ├── searcher.py              # GoogleLensSearcher：三段流程编排与重试
    ├── service.py               # LensSearchService：延迟启动与空闲关闭
    └── formatter.py             # 结果格式化
```

### 1.3 检索流程的划分

一次检索由上传、抓取、链接还原三段组成，各段的执行位置不同。这个划分是核心约束，
改动前务必先读 DESIGN_NOTES 第 2 节。

| 步骤 | 实现 | 执行位置 |
| --- | --- | --- |
| 1. 上传 | 向 Lens 首页的 `input[type=file]` 写入文件，等待 Google 跳转至带 `vsrid` 的结果页 | 浏览器内 |
| 2a. 完全匹配 | 将地址的 `udm` 改为 48，重新导航，渲染完成后在页面上下文执行提取脚本 | 浏览器内 |
| 2b. AI 描述 | 将 `udm` 改为 50，等流式回答收敛后提取正文 | 浏览器内 |
| 3. 链接还原 | 请求 `/goto?url=...` 读取 302 响应的 `Location` | httpx（带代理） |

`udm` 取值：26 全部、44 视觉匹配、48 完全匹配、50 AI 模式。

**两种结果模式是正交的**，由 `exact_matches` 与 `ai_mode` 两个开关独立控制，默认
都开。同一个 `vsrid` 换 `udm` 就能切标签页，所以两个模式共用一次上传，开两个只
多渲染一个页面。都关会在 `_search_once()` 里直接抛 `ParseError`，避免白跑一次上传。

`safe` 参数始终显式带上（默认 `safe=off`）—— 它的默认值随出口 IP 所在地区变化，
而 `safe=active` 会把命中过滤的结果**完全清空**，症状和「图片没被收录」无从分辨。

OCR 是一条独立链路：`POST /v3/upload` 与 `GET /qfmetadata` 两步必须共用同一个
HTTP 会话，由 `LensSession` 承担，与浏览器流程互不影响。

### 1.4 关键契约

**`RawMatch` → `ExactMatch`**：`parser.py` 从页面提取出的 `RawMatch` 中，`url` 与
`goto` 二者至少有一个有值。`goto` 为 Google 跳板链接，必须经第 3 段还原。
`searcher.py::_resolve()` 负责合并两者，未还原出地址的个别条目会被丢弃；页面已有
候选但最终一条地址都没有时抛 `FetchError`，避免把链路故障伪装成正常的“无结果”。

**异常边界**：输入、上传、抓取、限流、解析、浏览器不可用与忙碌错误均继承
`ImageSearchError`；其中只有 `ImageInputError` 携带由 `loader.py` 生成的受控用户说明，
其它异常的内部文字只进入脱敏日志。`SearchTimeoutError` 单独继承 `TimeoutError`，保留
标准超时语义。`main.py` 在插件边界按异常类型映射固定文案，未预期异常只记录脱敏摘要。

**请求生命周期**：同一用户在取图、排队和搜索组成的完整流程内只能占一个名额；
服务另设 1 个执行槽和有限等待队列。总超时从取得执行槽后开始，用户冷却从实际搜索
完成后开始，未取得图片、浏览器尚未就绪或队列已满都不会消耗冷却。

**配置三分**：`plugin_config.build_config()` 将 AstrBot 的配置字典转换为
`SearchConfig`（搜索与浏览器）、`OutputOptions`（输出格式）、`PluginOptions`
（插件行为）三个对象，业务层只认前两者。

---

## 二、模块职责

### 2.1 插件入口 `main.py`

`ImageSearchPlugin` 继承 `Star`，负责：

- 构造 `build_config()` 与 `LensSearchService`，数据目录取自 `StarTools.get_data_dir()`
- 在 `__init__` 与 `on_astrbot_loaded` 两处调用 `_schedule_prepare()`，把浏览器安装
  放到后台执行。插件加载时可能没有运行中的事件循环，因此需要后者兜底
- 注册指令 `搜图`（别名 `soutu` / `sauce` / `以图搜图`）与管理员指令 `搜图状态`
- 注册 LLM 函数工具 `reverse_image_search`，参数为图片 http(s) 地址
- `_pick_image()` 从当前消息或被引用消息中取第一张图；取不到时经
  `_wait_for_image()` 等待补发，该路径依赖 `session_waiter`，旧版本 AstrBot 上会
  降级为提示文案。**`stop_event()` 只能作用于补发图片那条消息的事件**，若作用于
  原指令事件会导致后续结果无法送出，原因见 DESIGN_NOTES 7.1
- `_begin_user_request()` 按 `会话 + 发送者` 原子登记完整流程：同一用户已有请求时
  立即拒绝，再检查上一次实际搜索完成后的冷却；`_finish_user_request()` 释放流程，
  仅在搜索确实进入服务后记录完成时间，字典超过 256 项时清理过期记录
- `_search()` 将 `SearchTimeoutError` / `RateLimitedError` /
  `BrowserNotAvailableError` / `ImageSearchError` / 未预期异常分别转成文案；只有
  `ImageInputError` 会展示经脱敏的受控异常文字，其它业务异常均使用按类型固定的文案；
  `SearchBusyError` 保留到指令或 LLM 工具边界，以便明确提示未进入队列；
  `_run_search()` 在其上再包一层，返回拼好的纯文本，供 LLM 工具与校验脚本使用
- `_send_result()` 按配置投递：`use_forward_message` 为真时把
  `format_blocks()` 的每一块包成一个 `Node`、整体作为 `Nodes` 发出；为假则逐块
  发普通消息。合并转发是 QQ 特有能力，其他平台会抛异常，此时回退为普通消息
- **全程使用 `event.send()` 而非 `yield`**。`yield` 出去的结果要经过
  `result_decorate` 阶段，那里会按全局配置 `forward_threshold`（默认 1500 字）
  把长消息折叠成合并转发、按 `reply_with_quote` 加引用，导致同一指令的呈现方式
  随结果字数漂移；`event.send()` 直连平台适配器，绕过该阶段。详见 DESIGN_NOTES 7.2
- `terminate()` 取消后台安装任务，并以有界等待关闭服务及活动搜索

### 2.2 配置映射 `image_search/plugin_config.py`

`build_config()` 读取 schema 的四个分组（`search` / `browser` / `output` /
`limits`），输出三个 dataclass：

- `SearchConfig`：完整定义见 `config.py`，包含 CDP 开关、无头、代理、超时、重试、
  窗口尺寸、安装策略、`user_data_dir`、`browser_install_dir` 等
- `OutputOptions`：条数上限、序号/站点名/尺寸的显隐、抬头模板、空结果文案
- `PluginOptions`：`command`、`wait_image_seconds`、`user_cooldown_seconds`、
  `idle_close_minutes`、`working_hint`、`request_timeout_seconds`、
  `max_pending_requests`

`_int()` / `_bool()` / `_text()` 三个转换函数对非法值做兜底并夹取范围，因此 WebUI
里填入异常值不会导致插件崩溃。`profile` 与浏览器安装目录都落在插件数据目录下，
保证容器重建后仍然保留。

`PluginOptions.command` 为固定值，仅用于日志与提示文案 —— `@filter.command` 在导入
时即已确定，无法跟随配置变化，因此该项未暴露到 schema。

### 2.3 服务层 `image_search/service.py`

`LensSearchService` 将 `GoogleLensSearcher` 包装为适合常驻进程的服务：

- **有限准入**：`_admit()` 原子统计活动与等待请求。容量为 1 个执行槽加
  `max_pending_searches` 个等待名额（插件默认额外排队 1 个）；满载时抛
  `SearchBusyError`，不会无限堆积
- **执行超时**：请求先进入等待队列，取得 `_execution_lock` 后才启动总超时。到期后
  取消本次任务、摘除共享 searcher 并抛 `SearchTimeoutError`，排队时间不计入时限
- **延迟启动**：`_ensure_searcher()` 在首次检索时才创建 searcher 并启动浏览器
- **空闲关闭**：`_idle_watch()` 周期检查空闲时长，超过 `idle_close_seconds` 则关闭
  浏览器；关闭前会在执行锁内复核没有活动或排队请求。下次检索会重新拉起，`0` 表示常驻
- **状态探测**：`_probe()` 提供一个不启动浏览器的 `BrowserSession`，供
  `browser_ready()` 与 `install_status()` 使用。`install_status()` 会区分「自动安装
  完成」与「系统本来就有浏览器」两种就绪状态
- **预安装**：`prepare()` 只装浏览器、不启动，供插件加载后在后台调用
- **有界关闭**：关闭 searcher 最多同步等待 20 秒；仍未结束时让清理任务在后台继续，
  并取走其最终异常，避免卸载或超时恢复永久挂住
- **终态卸载**：公开 `close()` 的并发调用复用同一个关闭任务；首次调用后永久拒绝
  搜索和 OCR，调用方超时或取消不会取消底层清理。空闲回收只调用内部会话关闭，
  不会把服务置为终态

### 2.4 流程编排 `image_search/searcher.py`

`GoogleLensSearcher.search()` 持有 `asyncio.Lock`，同一实例的检索串行执行。

`_search_once()` 依次完成：调用 `browser.upload_and_extract()` 得到页面提取结果、
可选执行 OCR、`extract_items()` 解析卡片、`_resolve()` 还原链接、可选补全标题，
最后组装 `LensSearchResult`。

重试逻辑只针对 `RateLimitedError`：捕获后调用 `browser.reset_session()` 清除已被
标记的 cookie 并重新预热，等待 `retry_delay_s` 后重试，共 `max_retries + 1` 次。
其他异常直接向上抛出。

`upload()` 与 `ocr()` 是两个不启动浏览器的纯 HTTP 方法，仅用于连通性验证与 OCR。

### 2.5 浏览器进程管理 `image_search/chrome.py`

- `locate_chrome()` 按「配置路径 → `CHROME_PATH` → 插件安装目录 → 系统浏览器 →
  Playwright 默认位置」顺序查找，**同时返回逐步检查记录**，用于生成可操作的诊断
  信息。自动安装时该目录要求插件完成标记；关闭自动安装后允许管理员接管，只要求
  当前 revision 与 Playwright 完成标记。`browser_missing_message()` 据此拼装提示，
  包含手动安装命令与缺失库清单
- `ChromeProcess` 以常规 subprocess 启动浏览器并开放 CDP 端口，`start_new_session=True`
  使其位于独立进程组，退出时 `os.killpg` 整组回收
- `profile_for()` 按可执行文件哈希隔离 profile 目录
- `browsers_using_profile()` / `release_profile()` 处理残留进程与 `SingletonLock`：
  启动前清理一次，遇到退出码 21 再清理并重试一次
- `normalize_user_agent()` 将 UA 中的 `HeadlessChrome/` 替换为 `Chrome/`，版本号保持
  不变；结果由 `read_cached_user_agent()` / `write_cached_user_agent()` 缓存在 profile
  目录内，避免每次启动都重新探测

### 2.6 浏览器自动安装 `image_search/installer.py`

`BrowserInstaller.ensure()` 将下载与依赖安装拆成两步执行，避免任一环节失败导致
浏览器本体也装不上：

1. `playwright install chromium` 只下载浏览器
2. 依赖优先用 `playwright install-deps`，失败则回退到自维护的 apt 包清单

`PLAYWRIGHT_BROWSERS_PATH` 仅在子进程环境中设置，不修改 `os.environ`，因为同进程
内可能存在其他使用 Playwright 的插件。

自动安装开启时，插件目录只接受与当前 Playwright 客户端 revision 完全一致、且同时
具有 Playwright `INSTALLATION_COMPLETE` 和插件完成标记的浏览器。取消或超时后即使
下载刚完成，也会先让自动安装流程补完依赖检查和插件标记。管理员关闭自动安装后，
标准 `playwright install` 只需提供当前 revision 与 Playwright 完成标记即可被接纳；
真正下载中断的目录仍不会被当作可用浏览器。

`InstallState` 描述安装状态（`IDLE` / `RUNNING` / `DONE` / `FAILED` / `CANCELLED` /
`SKIPPED`），`status_text()` 输出面向用户的文案，由 `/搜图状态` 展示。安装任务无论
成功、失败还是取消，`main.py::_prepare_browser()` 都会在 `finally` 中清理任务引用；
`FAILED` / `CANCELLED` 不会锁死 `ensure()`，下次状态检查或搜索可以重新触发。

`_spawn()` 的总时限同时覆盖子进程退出和 stdout drain；取消或超时后会把进程树清理
放入独立任务并屏蔽调用方的后续取消。POSIX 通过独立进程组发送 `SIGTERM` 后补发
`SIGKILL`；Windows 使用 `taskkill /PID /T /F`，因此仍假设父 PID 在清理开始时可查询。
若安装器将来需要支持“父进程先退出、后代继续持有 stdout”的任意命令，需改用创建时
绑定的 Windows Job Object，不能继续依赖事后按 PID 枚举。

### 2.7 页面操作 `image_search/browser.py`

`BrowserSession` 持有 Playwright、CDP 连接、浏览器进程与上下文：

- `start()` 每次复用前先调用 `_is_healthy()`，同时检查 context、底层 Chrome 进程、
  Playwright browser 连接，并用 3 秒 cookie probe 验证 CDP 通道；失效时先完整
  `_teardown()`，再重新启动
- 健康检查未通过后的启动流程经 `_start_cdp()`：定位浏览器、按需自动安装、启动进程、
  必要时探测并修正 UA 后重启一次、`connect_over_cdp()` 附加、
  `_prepare_context()` 注入 `SOCS` cookie 并按需预热。`_start_playwright_launch()`
  仅作后备，正常路径不会走到
- `upload_and_extract()` 完成第 1、2 段：打开 Lens 首页、`_dismiss_consent()` 处理同意
  弹窗、`_submit_image()` 提交图片，然后按 `exact_script` / `ai_script` 是否传入决定
  抓哪些标签页，结果装进 `LensPageResult`（`exact_payload` / `ai_payload`，没抓的
  保持 `None`）
- `_collect_ai()` 抓 AI 模式页。回答是流式输出的，打开页面后先用最多
  `ai_wait_ms` 等待正文开始生成，再单独用最多 `ai_wait_ms` 等待字数连续 3 轮不再
  增长（默认每阶段 30 秒，实测 11~12 秒收敛）。未生成正文或提取脚本持续失败时抛
  `ParseError`；双模式下由上层保留完全匹配结果
- `_submit_image()` 倒序尝试页面上的多个 `input[type=file]`；检测到 `vsrid` 后仍需
  等待 `networkidle` 与固定时长，等 Google 补全查询参数，否则据此改写出的完全匹配
  页没有结果
- `resolve_redirects()` 完成第 3 段，使用 httpx 并携带 `config.proxy`
- `reset_session()` 清除 cookie 并重新预热，供限流重试使用
- `_assert_not_blocked()` 在每个导航节点检查 `/sorry/`，命中即抛 `RateLimitedError`
- `_dump()` 在 `config.debug_dir` 已设置时保存 HTML 与可视区域截图
- Playwright driver 与 `asyncio.to_thread(chrome.start)` 在调用方取消后可能迟到完成；
  `_startup_cleanup_tasks` 会强引用“等待启动终态后立即停止”的独立任务，后续启动与
  `close()` 都先等待这些任务。重复取消只结束调用方，不会截断迟到资源回收

### 2.8 输入处理 `image_search/loader.py`

`load_image()` 把本地路径、http(s) URL、`bytes` 三种输入统一为
`(数据, 文件名, mime)`。类型只按文件头（`sniff_mime()`）识别，接受 PNG / JPEG /
GIF / WebP / BMP；扩展名不参与格式兜底，只在原名称没有后缀时用于补齐上传文件名。

`extract_first_frame()` 使用 Pillow 对所有输入做完整解码验证；GIF / 动态 WebP /
APNG 提取首帧并转为 PNG，静态图片验证后返回 `None`。有透明信息时转 RGBA，否则
转 RGB。Pillow 缺失、图片损坏、解码失败或格式不受支持时抛 `ImageInputError`，
不会把未经验证的原始数据继续上传。

大小判定分两道：入口 `MAX_SOURCE_BYTES`（64 MB）只防止解码耗尽内存；抽帧**之后**
再按 `MAX_IMAGE_BYTES`（20 MB）判定实际上传数据。顺序不能颠倒，否则大体积动图会
在抽帧前被拒。另以 `MAX_IMAGE_PIXELS` 将解码尺寸限制为 4000 万像素，防止压缩炸弹。

### 2.9 HTTP 与外部 URL 行为

`http_client.py` 提供不校验目标地址的普通 HTTP 流式读取：最多跟随 20 次重定向，
并让全部跳转与响应读取共享调用方给出的总时限。图片响应必须完整读取且不得超过
64 MB；标题补全只读取前 64 KiB。手工处理跳转是为了不让 HTTPX 把中间响应体完整
缓存在内存中，不包含域名、DNS、IP、端口或网络范围过滤。

浏览器、Lens HTTP 会话、外部图片下载、标题补全和跳板还原都会接收
`SearchConfig.proxy`。`browser.py::resolve_redirects()` 对同主机的中间跳转最多继续
3 次，遇到跨主机 `Location` 后只返回地址、不请求来源站点。

当前实现没有应用层 SSRF 隔离：外部图片 URL、标题补全 URL，以及传入跳板还原器的
初始或同主机跳转 URL，都可访问 AstrBot 进程或代理能够到达的回环、内网及云元数据
地址。这是为兼容 Fake-IP 和统一代理链路作出的明确取舍；部署方需要在代理、容器网络
或防火墙侧实施出口控制，并限制 LLM 工具的调用方。

`session.py` / `uploader.py` / `metadata.py` 负责 Lens 自身的 HTTP 链路。

`uploader.py` 提供 `build_upload_params()`（构造 Lens 网页版会携带的
`hl` / `re` / `stcs` / `vpw` / `vph` / `ep` 参数）与 `to_exact_matches_url()`
（改写 `udm` 与 `hl`）。

`LensSession` 把上传与 OCR 绑定在同一会话内。支持 httpx 与浏览器
`APIRequestContext` 两种传输：`upload()` 在传入 browser context 时优先走浏览器网络
栈并在失败时回退 httpx；`ocr_lines()` 同理。

`metadata.py` 解析 `qfmetadata` 返回的 JSPB 结构，还原 OCR 文本行。

### 2.10 结果解析 `image_search/parser.py`

`EXTRACT_SCRIPT` 是在页面上下文执行的 JavaScript，负责识别三种结果链接形态
（`/goto?` 跳板、`/url?q=` 老式跳板、直接的站外地址），过滤 Google 自有域名，
并输出每张卡片的文字行、`aria-label` 与缩略图。在页面内取数而非离线解析 HTML，
是因为 `innerText` 只包含真正可见的文字。

`_parse_lines()` 按「标题 / 日期 / 尺寸 / 站点名」的行结构拆解卡片文字，识别带千分位
的尺寸、多种日期格式，并过滤分隔符与导航噪声。

解析不依赖任何 class 名 —— Google 的 class 为哈希值且会轮换。唯一依赖的形态是
「跳板链接 + 文字行结构」。

`extract_items()` 输出 `RawMatch` 列表；`parse_extracted()` 是仅用页面内信息构造结果的
离线版本，供测试使用。

AI 模式另有一套，分工是「浏览器只定位，Python 做判断」：

- `AI_EXTRACT_SCRIPT`（页面内执行）划定回答子树，优先
  `[data-subtree="aimc"]` 里的 `[data-container-id="main-col"]`，并给
  `display:none` / `visibility:hidden` 的元素打上 `data-is-hidden` 标记 ——
  可见性只有浏览器算得出来，HTML 里看不出来。之后返回整块 `outerHTML`
- `ai_html_to_text()`（Python 侧，bs4）把这段 HTML 还原成纯文本。放在 Python 侧
  是为了能离线跑、能用合成 HTML 写断言

`ai_html_to_text()` 的步骤，全部按 DOM 结构判断，不看 AI 说了什么：

1. `_truncate_at_related_grid()` 在「相关内容」图片网格处截断。版面顺序固定为
   正文 → 网格 → 追问建议 → 分享面板，而网格是唯一形态上可辨的块（几十个
   `<img>` 加 `<a>`，正文段落各只有一个）。命中就连同后面的兄弟一起删掉，
   追问建议和分享面板一并消失。网格并非每次都出现（实测 5 份页面命中 2 份），
   没有网格时不做截断，追问建议会留在输出里 —— 原因见 DESIGN_NOTES 2.10
2. `_AI_DROP_SELECTORS` 删掉本来就不是正文的元素：`[data-is-hidden]`、
   引用来源胶囊（`<button>`）、`[aria-hidden="true"]`、图片
3. 按标签语义转文本：`role="heading"` 与 `h1`~`h6` 当小标题前后留空行，`li`
   加项目符号，`<table>` 的行转成「字段：值」或竖线分隔，其余块级元素各占一行
4. `_trim_dangling_tail()` 削掉「以下是更多相关内容：」这类因网格被删而悬空的
   冒号断尾

追问建议与结尾提问**一律原样保留**：它们与正文在 DOM 结构上不可区分，按措辞或
标点判断均不足够可靠（网格出现率约半数，剩余情形中追问的收尾形态在「问句」与
「引导语 + 列表」之间随机切换）。取舍依据见 DESIGN_NOTES 2.10

截断必须发生在删噪之前 —— 网格里的内容大半带隐藏标记，删噪后它就是个空 `div`。
认不出网格时不做任何截断，宁可多输出几行追问建议，也不拿措辞去赌：误判的方向是
砍掉正文，而放过追问只是多几行说明文字。依据与试过的其他判据见 DESIGN_NOTES 2.10。

### 2.11 输出与辅助模块

- `titles.py`：`complete_titles()` 抓取目标页 `<title>` 补全被截断的标题。仅当目标页
  标题以截断前缀开头时才替换，避免把跳转首页或反爬页的标题写进结果
- `formatter.py`：输出分两层。`format_blocks()` 把结果拆成**独立的消息块列表**，
  拆分粒度由 `OutputOptions` 控制；`format_result()` 把这些块用空行拼成一段文本，
  供本地脚本与 LLM 工具使用（它强制不拆链接，免得分隔块变成正文里的横线）。
  单条结果按 `标题` / `来源` /（可选 `尺寸`）/ `链接` 排列，链接固定在最后一行
  方便复制。插件与本地脚本共用同一套逻辑。注意展示标签与 `ExactMatch` 的字段名
  （`url` / `content` / `source`）是两回事，改文案不影响字段

  三个开关的组合语义：

  | 开关 | 作用 |
  | --- | --- |
  | `merge_ai_and_exact` | AI 描述是否并入完全匹配的第一块。与合并转发无关 |
  | `link_as_separate_message` | 每条结果占三块：序号头（`index_label`，默认 `[ 结果{index} ]`，受 `show_index` 控制）、「`标题（来源）`」、裸链接。序号头兼作结果之间的分隔，比单独插一条横线更有信息量。不输出字段名 —— 每条已是独立气泡，字段名是噪声，链接带前缀还会让长按复制多带一截。**仅在合并转发下生效** —— 普通消息逐条发会刷屏，所以 `format_blocks()` 里要求 `use_forward_message` 同时为真 |
  | `use_forward_message` | 由 `main.py` 消费：真则打包成一条合并转发，假则逐块发普通消息 |
- `models.py`：`ExactMatch` 提供 `truncated` 属性与 `format()`；`LensSearchResult`
  实现 `__bool__` 与 `__len__`，可直接用于真值判断
- `logger.py`：优先使用 AstrBot 的 logger，独立运行时回退标准库。另提供
  `quiet_http_logs()` 与 `quiet_image_logs()`：在现有 handler 上安装共享 filter，
  用 `ContextVar` 只压低当前 task/thread 作用域内 httpx / httpcore / PIL 的低级别
  日志，不永久修改第三方 logger level，也不影响同进程其它插件；上下文会随
  `asyncio.to_thread()` 传播。`sanitize_log_text()` / `exception_for_log()` 会清理 URL
  路径与查询参数、按字段语义识别的 snake/kebab/camel case 凭据、本地绝对路径、
  Cookie 容器与私钥块；多行敏感值从命中点起保守遮蔽。未预期异常不记录未经处理的
  traceback

---

## 三、主流程时序

以「用户发送 `/搜图` 并附带图片」为例：

1. `main.py::search_image()` 校验结果模式，`_begin_user_request()` 拒绝同一用户的重复
   in-flight 流程或尚未结束的完成后冷却
2. `service.browser_ready()` 为假时触发或复用后台安装并返回进度；该路径不启动冷却
3. `_pick_image()` 取图，必要时 `_wait_for_image()` 等待补发；发送 `working_hint` 后
   `convert_to_file_path()` 得到本地路径
4. `service.search()` 先经 `_admit()` 占用活动或等待名额；队列满时抛
   `SearchBusyError`，不进入搜索也不启动冷却
5. 请求取得 `_execution_lock` 后才启动 `request_timeout_seconds` 总超时，再经
   `_ensure_searcher()` 首次启动或复用浏览器
   （定位或安装 → 启动进程 → UA 修正 → CDP 附加 → 注入 cookie → 预热）
6. `searcher.search()` 取内部锁，`load_image()` 校验并归一化输入（动图在此提取首帧），
   `browser.start()` 复核已有会话健康状态
7. `browser.upload_and_extract()`：打开 Lens 首页 → 处理同意弹窗 → 提交图片 →
   等待 `vsrid` 与参数补全 → 改写 `udm=48` 并导航 → 等待渲染与滚动 → 执行提取脚本
8. `extract_items()` 得到 `RawMatch` 列表
9. `_resolve()` 经 httpx 还原跳板链接；保留成功条目，候选全部还原失败时抛 `FetchError`
10. `complete_titles()` 按需补全被截断的标题
11. 服务释放执行权、更新 `_last_used` 并释放准入名额，`main.py` 格式化并用
    `event.send()` 投递结果或错误文案
12. `_finish_user_request()` 释放用户流程；只要搜索已进入服务并返回，就从此时开始冷却
13. 空闲超过阈值后 `_idle_watch()` 在确认没有活动或排队请求时关闭浏览器

任一导航步骤检测到 `/sorry/` 即抛 `RateLimitedError`，由 `searcher.search()` 清会话
后重试。

---

## 四、错误处理

异常体系集中在 `exceptions.py`。预期业务错误继承 `ImageSearchError`，执行超时单独
继承 `TimeoutError`：

| 异常 | 触发场景 | 用户可见文案 |
| --- | --- | --- |
| `ImageInputError` | 图片为空、格式或大小非法、外部 URL 下载失败 | 展示受控的输入错误说明 |
| `UploadError` | 上传未返回重定向、页面结构变化导致找不到输入框 | 固定的图片上传失败提示 |
| `FetchError` | 结果页导航失败、`qfmetadata` 非 200、候选链接全部还原失败 | 固定的结果获取失败提示 |
| `RateLimitedError` | 命中 `/sorry/index`（继承 `FetchError`） | 提示触发人机验证，建议稍后重试或更换代理 |
| `ParseError` | 提取脚本返回类型异常 | 固定的页面结构变化提示 |
| `BrowserNotAvailableError` | 找不到浏览器、启动失败、缺 Playwright 或版本不一致 | 通用启动失败提示；细节写入脱敏日志 |
| `SearchBusyError` | 服务关闭中，或执行槽与有限等待队列均已占满 | 直接返回具体忙碌原因 |
| `SearchTimeoutError` | 取得执行权后的搜索超过总时限 | 提示超时、会话已重置并建议查看日志 |

`main.py::_search()` 负责转换异常；`SearchBusyError` 重新抛到指令或 LLM 工具边界，
其余预期错误变成用户文案。未预期异常只用
`logger.error(..., exception_for_log(exc))` 记录脱敏摘要，不写可能包含 URL、路径或
凭据的原始 traceback，然后统一返回「搜索出错了，详情见 AstrBot 日志」。

页面已有候选但跳板链接全部还原失败时会返回明确的抓取错误，不再伪装成“没有结果”。
部分链接失败时保留成功条目，并在日志中记录失败统计。

---

## 五、开发与测试

### 5.1 本地测试边界

测试脚本、测试用例与素材统一保存在仓库根目录的本地 `test/` 中，该目录由
`.gitignore` 排除，不属于插件发布内容。修改代码后只运行覆盖本次改动的定向测试；
需要真实 Google 请求的验证必须低频执行，并与离线测试分开。

### 5.2 管理员排查脚本

```bash
python tools/diagnose.py --image /path/to/image.png
python tools/diagnose.py --image /path/to/image.png --bundled
python tools/clash_api.py groups    # 默认仅显示组数量、类型与候选数
python tools/scan_nodes.py          # 逐节点实测可用性，结束后自动恢复
python tools/solve_captcha.py       # 人工完成一次验证码
```

`tools/clash_api.py list` / `groups` 默认隐藏代理组名、节点名和当前节点名；只有明确传入
`--show-names` 才显示名称，`switch` 的结果也不会回显传入名称。

### 5.3 依赖约束

`requirements.txt` 中四项依赖的作用与约束：

- `httpx`：链接还原、OCR 链路、标题补全、图片 URL 下载
- `playwright`：**版本属于功能性锁定**。它决定自动下载的浏览器版本，而过旧的浏览器
  会被 Google 判定为可疑客户端并拦截。升级该依赖时需同时确认下载到的浏览器版本
  足够新，实测数据见 DESIGN_NOTES 2.2
- `pillow`：对所有图片做完整解码与像素限制，并为动图提取首帧；缺失时拒绝输入，
  不允许绕过验证继续上传
- `beautifulsoup4`：按 DOM 结构把 AI 模式回答还原为纯文本

### 5.4 扩展指引

- **调整输出格式**：改 `formatter.py`，插件与本地脚本会同步生效
- **适配 Google 改版**：先运行 `tools/diagnose.py` 查看原始文字行，再调整
  `parser.py::_parse_lines()` 的启发式规则。`EXTRACT_SCRIPT` 只依赖链接形态，
  通常不需要改动
- **新增配置项**：同时改 `_conf_schema.json` 与 `plugin_config.py::build_config()`，
  并在本地 `test/` 中补充配置映射断言
- **改动核心流程**：先读 DESIGN_NOTES 第 2、3 节。浏览器启动方式、上传路径与链接
  还原方式都有实测约束，看似合理的简化通常已经被验证过不可行

---

## 六、部署与运维

本节面向管理员，README 只保留终端用户能自行处理的内容。

### 6.1 手动管理浏览器

宿主机已安装 Chrome / Chromium / Edge 时插件会优先使用系统浏览器。若需完全接管
浏览器管理，关闭**浏览器 → 自动安装浏览器**后手动安装：

```bash
PLAYWRIGHT_BROWSERS_PATH=/AstrBot/data/plugin_data/astrbot_plugin_image_search/ms-playwright \
  python -m playwright install --with-deps chromium
```

`--with-deps` 不可省略：精简镜像缺少 `libnss3` / `libatk-1.0` / `libcups` 等库，
仅下载浏览器会导致下载成功但无法启动。关闭自动安装后，插件会按当前 Playwright
revision 和 `INSTALLATION_COMPLETE` 接纳该手动安装，不要求插件私有完成标记。

也可通过配置项 `chrome_path` 或环境变量 `CHROME_PATH` 指定可执行文件。查找顺序为：
`chrome_path` → `CHROME_PATH` → 插件自装目录 → 系统 Chrome/Chromium/Edge →
Playwright 默认位置。

### 6.2 验证代理连通性

在 AstrBot 容器内执行，将地址替换为实际配置值：

```bash
PROXY_HOST='<proxy-host>' PROXY_PORT='<proxy-port>' python3 -c "
import os
import socket
s = socket.create_connection(
    (os.environ['PROXY_HOST'], int(os.environ['PROXY_PORT'])), timeout=5)
s.sendall(b'CONNECT lens.google.com:443 HTTP/1.1\r\nHost: lens.google.com:443\r\n\r\n')
print(s.recv(128).decode().splitlines()[0])"
```

返回 `HTTP/1.1 200 Connection established` 表示代理可用。

Docker 部署时代理地址不能填代理容器名（通常不在同一 docker network）。可用地址：

- 宿主机的局域网地址，如 `http://<host-address>:<port>` —— 推荐，稳定性最佳
- 当前容器网络的网关，如 `http://<gateway-address>:<port>` —— docker 网络重建后可能变化

### 6.3 症状与内部机制对照

终端用户看到的现象往往对应特定的内部环节，下表用于快速定位：

| 现象 | 内部原因 | 处理 |
| --- | --- | --- |
| 提示“页面抽取到候选，但所有来源链接都还原失败” | 第 3 段跳板还原全部失败，通常是代理或 Google 跳板链路异常 | 检查代理对容器是否可达，并查看脱敏日志中的失败类型 |
| 提示等待队列已满或当前已有任务 | 1 个执行槽和配置的等待名额都已占用，或同一用户已有 in-flight 流程 | 等现有任务结束；需要时调整 `max_pending_requests` |
| 「找不到可用的浏览器」 | 浏览器未安装、安装不完整或当前 Playwright revision 不匹配 | 发送 `/搜图状态`；已启用自动安装时等待或重新触发，细节查看脱敏日志 |
| 浏览器自动安装已取消或失败 | 安装子进程被取消、超时或退出异常 | 下次 `/搜图状态` 或搜索会重新触发，不需要清理状态文件 |
| 「浏览器启动后立即退出（code=21）」/ `SingletonLock: File exists` | 上一次浏览器进程未正常退出，profile 被锁定 | 可自动恢复：启动前清理占用进程，遇 21 再清一次并重试。持续出现说明进程回收异常 |
| 已运行的浏览器被外部关闭或 CDP 断开 | 复用前健康检查失败 | 插件会先回收旧会话再自动重建；反复发生时检查进程退出原因 |
| 浏览器下载成功但无法启动 | 缺少系统依赖库 | 确认已启用「自动安装时装系统依赖库」且容器内为 root；或执行 `python -m playwright install-deps chromium` |
| 持续触发人机验证 | 出口 IP 被限流 | 实测一小时内约 80 次检索后会持续返回 `/sorry/index`，约十分钟后恢复。压力测试易触发，正常使用配合冷却不会 |
| 容器重建后浏览器丢失 | 插件数据目录未落在挂载卷内 | 官方镜像挂载 `/AstrBot/data`，插件数据目录位于其下；自定义 `browser_install_dir` 时需确认路径 |

更细的判别方法（按耗时、按 HTML 特征、落盘现场）见 DESIGN_NOTES 第 5 节。

### 6.4 升级注意

`requirements.txt` 中 `playwright` 的版本决定自动下载的浏览器版本，过旧的浏览器会
被 Google 判定为可疑客户端并拦截。**请勿降低该版本**；升级时需确认下载到的浏览器
版本足够新，实测数据见 DESIGN_NOTES 2.2。

修改插件文件后，AstrBot 主进程内已导入的模块不会自动替换，需在 WebUI 重载插件或
重启容器才会生效。

### 6.5 升级 playwright 后必须重启 AstrBot

**这是一个实际发生过的故障，症状极具误导性：用户只收到「正在搜索，请稍候……」，
之后再也没有任何回复；此后每一次搜索都同样无响应。**

成因链条：

1. AstrBot 在自己的进程内用 pip 安装插件依赖。某次安装把 playwright 从 1.49
   升级到 1.62，磁盘上的 Python 客户端与 node driver 都换成了新版
2. 进程里早先 `import` 的 1.49 客户端仍在 `sys.modules` 中。**重载插件不会替换
   它** —— 重载只重新导入插件自己的模块
3. 1.49 客户端启动 1.62 的 driver，driver 的初始化消息里不再包含 `selectors`
   字段，客户端在 `playwright/_impl/_playwright.py` 读取该键时抛
   `KeyError: 'selectors'`
4. 这个异常发生在 `Connection.run()` 的后台任务里，主流程拿不到，日志中只留下
   一条 `Task exception was never retrieved`
5. 此后所有 Playwright 调用都在等一个永远不会完成的 future。**Playwright 的超时
   由 driver 端实现，客户端收不到任何消息就永远不会超时**，代码里传的
   `timeout_ms` 完全无效
6. `GoogleLensSearcher` 的锁被永久持有，后续每次搜索都卡在等锁，连日志都不再输出

判别方法：

```bash
# 日志里出现这一条即可确认
docker logs YOUR_ASTRBOT_CONTAINER 2>&1 | grep "KeyError('selectors')"
# 进程内版本 vs 磁盘版本（新进程里三者应一致）
docker exec -i YOUR_ASTRBOT_CONTAINER python3 -c "
import importlib.metadata as m
from playwright._repo_version import version
print('进程内', version, '/ 磁盘', m.version('playwright'))"
```

解决办法只有一个：**重启 AstrBot 或重启容器**。重载插件无效。

代码层面已做两道防护：

- `browser.py::playwright_version_mismatch()` 在启动浏览器前比较
  `playwright._repo_version.version`（进程内模块）与
  `importlib.metadata.version("playwright")`（磁盘 dist-info）。不一致时直接抛
  `BrowserNotAvailableError` 并提示重启，不再让它挂死
- `service.py::_run_operation()` 在请求取得执行权后启动总超时（配置项「单次搜索总
  超时」，默认 180 秒）。到期后取消搜索任务、先摘除共享 searcher 引用，再关闭对应
  会话；关闭同步等待 20 秒后转后台继续清理，避免恢复流程无限卡住。
  `main.py::_search()` 单独捕获 `SearchTimeoutError` 并返回明确提示
