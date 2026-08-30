# 实现思路与排错备忘

本文件记录**为什么是现在这个样子**：试过哪些路、哪些被否决、数据在哪、还有什么
没搞清。模块职责与主流程见 [ARCHITECTURE.md](ARCHITECTURE.md)，安装配置与排错见
[README](../README.md)。

文中每条结论都标了来源：**实测**表示在真实环境跑出过数据，**推断**表示只是
合理猜测、没有验证过。区分这个很重要 —— 本项目已经有三条"看起来合理"的结论
被后来的实验推翻，见〈被推翻的结论〉。

---

## 1. 整体链路

模块划分和流程时序在 [ARCHITECTURE.md](ARCHITECTURE.md) 里，这里只回顾与后文
决策相关的部分：一次搜索三段，三段的执行位置不一样，这是踩了坑之后的结果，
不是随手定的。

| 步骤 | 做法 | 执行位置 | 关键约束 |
| --- | --- | --- | --- |
| 1. 上传 | 把文件塞进 Lens 首页的 `input[type=file]`，Google 自己跳到带 `vsrid` 的结果页 | **浏览器内** | `vsrid` 绑定上传方身份，换客户端上传会 "Expired visual search" |
| 2. 渲染结果页 | 把地址的 `udm` 改成 48，重新打开，等渲染完在页面里抽卡片 | **浏览器内** | 没有有效会话 cookie 时纯 HTTP 只能拿到 92 KB 引导壳 |
| 3. 还原链接 | `GET /goto?url=...` 读 302 的 `Location` | **httpx + 代理** | `context.request` 拿不到浏览器的代理设置 |

`udm=26` 是「全部」，`udm=48` 是「完全匹配」。

附带的 OCR 是独立的一条路：`POST /v3/upload` + `GET /qfmetadata`，纯 HTTP，
两步必须共用同一个会话（换客户端只会拿到 136 字节空响应）。由 `LensSession`
负责，和浏览器流程互不干扰。

这三段分别落在 `browser.py::upload_and_extract()`（前两段）和
`browser.py::resolve_redirects()`（第三段），由 `searcher.py::_search_once()` 编排。

---

## 2. 关键决策

### 2.1 浏览器必须自己启动，不能让 Playwright launch

Playwright 启动时带 `--enable-automation` 等标记，botguard 能识别，把 `/search`
转到 `/sorry/index`。

| 启动方式 | 结果 |
| --- | --- |
| `launch_persistent_context(channel="chrome")` | `/sorry/index`（实测） |
| 普通 subprocess 启动 + `connect_over_cdp` | 正常拿到结果（实测） |

这个坑一开始被误判成 IP 信誉问题：8 个代理节点（东京 / 香港 / 新加坡 / 美国）
逐个试，浏览器全被拦；换真实 Chrome 通道、预热 profile、`playwright-stealth`
都无效。改成 CDP 附加后**同一批 IP 立刻全部可用**。

教训：换一个维度试之前，先确认当前维度真的是变量。

### 2.2 浏览器版本不能太旧，而版本由 playwright 的版本决定

同一出口 IP、其他条件相同的交替对比（实测）：

| 浏览器 | 来自 | 通过率 |
| --- | --- | --- |
| Chromium 131 | playwright 1.49 自带 | 0/5 |
| Chrome for Testing 151 | playwright 1.62 自带 | 5/5 |

所以 `requirements.txt` 里 `playwright==1.62.0` 是功能性的钉版，改它要同时确认
下载到的浏览器版本。

试过的替代方案，都不用：

* 装真实 Google Chrome —— `playwright install chrome` 从 `dl.google.com` 拉 `.deb`，
  超过 30 分钟没完成，容器里不实用
* 伪装 WebGL 渲染器（SwiftShader 报成 Intel GPU）—— 0/2，无效

新旧 playwright 的目录布局不同（新版 `chrome-linux64/`，旧版 `chrome-linux/`），
`installer.py::find_chromium_in()` 对两种都认，跳过 `chromium_headless_shell-*`，
多版本取 revision 最高的。

### 2.3 无头 UA 要改，但版本号必须和浏览器真实版本一致

无头模式的 UA 带 `HeadlessChrome`，这一条就够被拦。但写死 UA 更糟 —— 版本对不上
反而更可疑（实测）：

| 组合 | 结果 |
| --- | --- |
| Chromium 131 无头 + 默认 UA（含 `HeadlessChrome/131`） | 被拦 |
| Chromium 131 无头 + UA 写 `Chrome/131` | 通过 |
| Chromium 131 有头 | 通过（有头 UA 本来不带 Headless） |
| Chrome 152 无头 + UA 写 `Chrome/131` | **被拦**（版本对不上） |
| Chrome 152 无头 + UA 写 `Chrome/152` | 通过 |

做法：启动后从 CDP `/json/version` 读浏览器自报的真实 UA，只把
`HeadlessChrome/` 替换成 `Chrome/`（版本原样保留），带 `--user-agent` 重启一次，
结果缓存进 profile 目录。

### 2.4 上传走浏览器里的真实 UI

`vsrid` 绑定上传方身份（实测）：

| 上传方式 | 结果 |
| --- | --- |
| 独立 httpx 客户端 POST `/v3/upload` | Expired visual search |
| `context.request` POST（playwright 1.49） | 可用 —— 那时它和浏览器共享会话 |
| `context.request` POST（playwright 1.62） | Expired visual search，**已不再共享** |
| 浏览器里 `set_input_files` | 稳定可用（同图 20 条 vs 0 条） |

`_submit_image()` 里有个容易忽略的点：跳转后不能一看到 `vsrid` 就拿地址。
Google 会**逐步补全**查询参数（`gsessionid` / `lsessionid` 等），拿早了得到的是
不完整地址，按它改写出的完全匹配页一条结果都没有。所以等 `networkidle` 再多等
4 秒才读 `page.url`。

页面上有多个 `input[type=file]`，只有一个是 Lens 的。实测它通常排在最后，所以
倒序尝试；探测阶段的等待时间给短一点，避免在错误的 input 上白等。

### 2.5 还原链接用 httpx + 代理，不用 `context.request`

**这是 Docker 容器里"抽到 20 张卡片却返回 0 条结果"的根因。**

CDP 附加的上下文不会把浏览器的 `--proxy-server` 转给 `context.request` ——
那些请求由 Playwright 进程直连发出。容器里没有系统代理，于是全部超时；而
`_resolve()` 会丢掉没还原出地址的条目，最终表现就是 0 条。

同一环境、同一批链接（实测）：

| 方式 | 成功 | 耗时 |
| --- | --- | --- |
| `context.request` | 0/5 | 60.0s（跑满超时） |
| httpx + `config.proxy` | 5/5 | 0.6s |
| httpx + proxy + 浏览器 cookie | 5/5 | 0.6s |
| `page.goto` 逐个打开读地址 | 3/3 | 4.0s |

顺带确认跳板解码**不依赖会话**，不带 cookie 也能还原，所以不用从浏览器导
cookie。超时收到 20s：实测 0.6s 就够，长超时只会让个别卡住的链接拖慢整批。

这个 bug 的症状很有迷惑性 —— 不报错、不弹验证码、日志干净，只是结果为空。
**判别方法看耗时**：正常一次 20~30s，全超时会变成 80s 以上。

### 2.6 profile 按浏览器隔离，并处理孤儿进程

* 不同版本浏览器共用 profile 会起不来（Chrome 152 写过的 profile 给 Chromium 131
  会直接拒绝启动），所以 `profile_for()` 按可执行文件路径哈希分目录
* 浏览器没退干净会留下 `SingletonLock`，下次启动退出码 21。`browsers_using_profile()`
  找出仍占用该 profile 的进程，启动前清一次，遇到 21 再清一次并重试
* 浏览器以独立进程组启动（`start_new_session=True`），关闭时 `os.killpg` 整组收，
  避免留一堆孤儿 Chrome 进程

### 2.7 撞过验证码的 cookie 要清掉再重试

Google 的判定带随机性，但一旦撞上验证码，这个 profile 的 cookie 就被标记了
（实测）：

| 场景 | 成功率 |
| --- | --- |
| 每次用全新 profile | 4/4 |
| 复用撞过验证码的 profile，原样重试 | 0/4 |
| 复用撞过验证码的 profile，重试前 `clear_cookies` | 5/5 |

所以 `reset_session()` 在重试前清 cookie 并重新预热。

注意：容器里做过两轮 reuse vs fresh profile 的对比，两轮结论相反（0/6 vs 6/6，
然后 5/5 vs 0/5），判定为概率性波动，没有据此改设计。

### 2.8 不需要 Google 账号

全新 profile 搜索正常，会话里只有 5 个匿名 cookie（`AEC` / `DV` / `SOCS` /
`__Secure-ENID` / `GOOGLE_ABUSE_EXEMPTION`），没有任何登录态 cookie（实测）。
profile 持久化只是为了复用匿名 cookie 降低触发验证的概率，丢了会自愈。

### 2.9 动图抽第一帧

要点：抽帧**不提高命中率**（实测原样上传和抽帧返回同一批结果，见 3.3），
收益是体积（1851 KB → 205 KB）和确定性。大小判定必须放在抽帧**之后**，
否则几十 MB 的 GIF 会被白白拒掉，而它的第一帧可能只有几百 KB。

入口另有一道 64 MB 的硬闸（`MAX_SOURCE_BYTES`），只为防止 Pillow 解码吃满内存。

### 2.10 AI 模式（udm=50）：正文边界靠 DOM 结构划

结果页顶部的标签栏对应不同的 `udm`，同一个 `vsrid` 换 `udm` 即可切换，**不用重新
上传**（实测）：

| 标签 | udm | 跳板链接 | 内容形态 |
| --- | --- | --- | --- |
| AI Mode | 50 | 13 | 自然语言段落，可能带小标题和表格 |
| All | 26 | 92 | 顶部也有 AI 摘要，下面是混合结果 |
| Exact matches | 48 | 360 | 卡片列表（完全匹配用的） |
| Visual matches | 44 | 58 | 相似图卡片 |

**匿名会话就能用 AI 模式**，不需要 Google 账号。回答是流式输出的，实测 11~12 秒
收敛，判定方式是「已开始生成 + 字数连续 3 轮不增长」。

难点全在提取 —— 回答区里不只有回答，还混着引用胶囊、「相关内容」图片网格、追问
建议、分享面板。

最初的做法是把回答区的 `innerText` 取回来逐行过正则：域名行丢掉、`· 8 years ago`
结尾的丢掉、「如果您对……感兴趣：」之后的全丢掉。这条路是错的，因为每条规则都在赌
Google 的措辞：漏掉一个敬语「您」就放跑三条追问建议，换成英文界面整套规则失效，
而正文里正常的「作品编号：ABCD-123」又会被域名规则误伤。

**现在只按 DOM 结构判断。** 页面脚本 `AI_EXTRACT_SCRIPT` 缩到只做两件事 —— 划定
回答子树、给不渲染的元素打 `data-is-hidden` 标记 —— 然后把 `outerHTML` 整块交给
`ai_html_to_text()` 用 bs4 处理。好处是这部分逻辑可以离线跑、能用合成 HTML 写断言，
而页面脚本里既不好调试也没法测。

**唯一可靠的分界是「相关内容」图片网格。** 回答带网格时，版面顺序固定为正文段落 →
相关内容网格 → 追问建议 → 分享／反馈面板。这几段的属性是同一套（都带 `data-hveid`），
class 又是轮换的哈希，唯一能认出来的是网格自身的形态：

| 块 | `<img>` | `<a>` |
| --- | --- | --- |
| 正文段落 | 1 | 1 |
| 相关内容网格 | 60 | 30 |
| 引导语 / 追问列表 | 0 | 0 |

差着一个数量级。命中网格就把它和后面的兄弟全部删掉，追问建议和分享面板一并消失，
不需要再判断哪句话是引导语。

顺序上必须**先截断再删噪**：网格里的图和文字大半带隐藏标记，删噪之后它就成了一个空
`div`，特征全没了。

**网格不是每次都出现。** 5 份实测页面里只有 2 份带网格 —— 同一张图重搜，有时给网格
有时不给，和图片无关。所以这条判据的覆盖率不是 100%：带网格时输出是纯正文，不带网格
时追问建议会留在结果里。

**没有网格时不再猜边界。** 追问建议和正文列表在结构上是真的分不开，逐个试过：

- `data-hveid`：两者都有，值是递增序号，没有语义
- `data-sae`：以为是「可点击建议」的标记，结果正文列表的 `li` 也带
- 嵌套层级：无网格的页面里追问是「`div` 内嵌 `ul`」、正文列表是顶层 `ul`；
  但带网格的页面里追问的 `ul` 也是顶层。两种形态都出现过，不能用
- 「引导语以冒号结尾」：正文里「详细信息：」后面跟列表是完全正常的写法
- 「条目全是问句」：带网格那批的追问是祈使句（`动画的经典台词与名场面`），不带问号

这些判据错判的方向都是**砍掉正文**，而放过追问建议只是多几行说明文字。结果直接发给
用户，多几行一眼能忽略，少了正文却看不出来，所以宁可多给。`verify_plugin.py` 里留了
一组对照断言：同一份 HTML 里，网格前的「冒号引导语 + 列表」必须保留，网格后的必须
丢弃。

**最后试过的方案是「结尾问句」，也放弃了。** AI 常在末尾追一句「你想进一步了解哪方
面的内容呢？」，只按位置和标点判断（从最后一行往前，末句以问号收尾就丢）看起来足够
稳 —— 问号是标点不是措辞，对回答语言不敏感。

但实测三张图，这条规则**一次都没命中**。因为追问的收尾形态在两种之间随机切换：

```text
形态 A：…（正文）        形态 B：…（正文）
你想了解哪方面的信息呢？   如果你想了解更多，我可以为你提供：
                        • 《DARLING in the FRANXX》动画的剧情简述
                        • 02 与其他角色的关系羁绊
                        请告诉我你接下来想了解的内容。
```

形态 B 的最后一行是列表项、收尾语是句号，问号规则完全够不着。而网格出现率本身只有
一半，两个概率叠起来，能清干净的情况是少数。

**所以最终决定：不再剔除，AI 回答原样搬运。** 只保留两项与追问无关的处理 —— 在
「相关内容」图片网格处截断（那是几十个站点名，纯噪声），以及补掉网格被删后悬空的冒号
断尾（`以下是更多相关内容：` 指向一个已经不存在的东西）。

理由是每一版剔除方案的失效方向都一样：清不干净只是多几行说明文字，用户一眼能忽略；
一旦误判就是砍掉正文，而正文少了看不出来。既然没有既干净又安全的判据，就选安全的
那边。

**不能用 `offsetParent === null` 判断隐藏。** Google 大量使用 `display: contents`，
这类容器本身没有盒子、`offsetParent` 天然为 null，但里面的内容是可见的。用它做过滤
条件（还配上 `FILTER_REJECT`，会砍掉整棵子树）的结果是 746 个元素只剩 25 个，正文
一个字都取不到。现在只认 `getComputedStyle().display === 'none'` 和
`visibility: hidden`。

**不能排除 `<a>` 里的文本。** 一开始为了滤掉引用卡片，跳过了所有 `<a>` 内的文本，
结果首句被切成三段 —— 因为作品名是**内联在句子里的链接**。引用来源那圈胶囊是
`<button aria-label="巴哈姆特（另有 6 个）">`，按标签删掉就行，不用碰 `<a>`。

**容器优先 `[data-container-id="main-col"]`。** `[data-subtree="aimc"]` 是整块回答
（3442 字），连底部引用卡片一起；`main-col` 是主内容列（3032 字），差值正好是那堆
卡片。

**`get_text("")` 不能写成 `get_text("\n")`。** 后者在每两个文本节点之间都插换行，
一句话会被内联的 `<strong>` 切成三行。换行只应该来自显式插入的块级边界。

**AI 会拒答，而且不稳定。** 同一张图，一次给出完整的作品信息，另一次回「抱歉，我
无法提供此图片中相关内容的详细信息」。拒答文案没有信息量，
命中 `_AI_REFUSAL_RE` 且短于 200 字就当作没有描述，让输出只保留完全匹配。这是整个
解析里唯一还靠措辞判断的地方 —— 「拒答」本身只能从语义上认出来。

### 2.11 SafeSearch 默认关闭，而且必须显式声明

`safe` 参数的实测对照（同一出口 IP）：

| 设置 | 命中过滤的图 | 其他图 |
| --- | --- | --- |
| 不带 `safe` 参数 | 50 条 | 50 条 |
| `safe=off` | 50 条 | 50 条 |
| **`safe=active`** | **0 条** | 50 条 |

`safe=active` 把命中过滤的结果**完全清空**（不是部分过滤），其他图不受影响。

匿名会话的默认值等于 `off`，所以不带参数也能搜到。但这个默认值由 Google 按出口 IP
所在地区判定，部分地区强制开启 —— 一旦被强制，症状是「返回 0 条」，和「图片没被
收录」完全一样，无从分辨。所以 `to_mode_url()` 始终显式带上 `safe`。

顺带一个诊断陷阱：页面上「安全搜索」这几个字在任何情况下都存在（那是设置菜单），
拿它当「被过滤」的信号会一律误报。

### 2.12 自动安装：下载和装系统依赖要分两步

`playwright install --with-deps chromium` 是一条命令做两件事，但它的 apt 包清单
是硬编码的，遇到发行版对不上就整条命令失败 —— 实测 Debian 13 上报
`E: Package 'ttf-unifont' has no installation candidate`，然后
`Error: Installation process exited with code: 100`，**浏览器本体也没装上**。

所以 `installer.py` 把两步拆开：先 `playwright install chromium` 只下载，
再单独装依赖。装依赖优先用 `playwright install-deps`，失败则回退到自己维护的
apt 清单（22 组包名，含 Debian 13 的 `t64` 后缀候选）。清单刻意**不含字体包**
（`ttf-unifont` / `ttf-ubuntu-font-family`）—— Debian 13 没有这些包，而且它们
和能否启动无关。实测 22/22 可用、`--dry-run` 退出码 0。

另外两个细节：

* 装到插件数据目录（挂载卷）而不是 `~/.cache`，容器重建不会丢
* `PLAYWRIGHT_BROWSERS_PATH` 只在子进程环境里设，不动 `os.environ` ——
  同进程里可能有别的插件也在用 Playwright

依赖库检测曾有个大小写 bug：X11 的库名是 `libXcomposite.so.1`（大写 X），
按小写比对会误报缺失。现在比对是大小写不敏感的。

---

## 3. 被推翻的结论

这一节比上面更重要。三条曾经写进代码注释或 README 的结论，后来被实验证否。

### 3.1 「结果页必须用浏览器渲染，纯 HTTP 只能拿到引导壳」—— 不成立

原始依据是真实的：Google
[自 2025-01-15 起要求 Search 页面执行 JavaScript](https://techcrunch.com/2025/01/17/google-begins-requiring-javascript-for-google-search/)，
无 JS 客户端只能拿到约 90 KB 的引导脚本壳。为了绕过它试过下面这些，**全部无效**：

* 换 UA 伪装老浏览器（旧 Firefox / IE8 / Lynx 只会得到「Update your browser」）
* `curl_cffi` 模拟 Chrome 的 TLS / JA3 指纹
* `gbv=1`（老版无 JS 界面开关）
* 跟随页面里的 `enablejs` 恢复链接
* 换区域域名（`google.co.jp` / `.com.hk` / `.com.sg` / `.de`，返回完全一样）

也确认过 `lens.google.com` 上没有直接返回匹配结果的 JSON 接口：除了
`/v3/upload` 和 `/qfmetadata`，`/metadata`、`/results`、`/matches`、
`/exactmatches` 等路径全是 404，`batchexecute` 也没有对应的 boq 应用。
`qfmetadata` 只返回 OCR 文字和区域坐标，没有匹配结果。

这些实验本身没做错，错在推论：从「无 JS 客户端拿不到」直接跳到了「必须用浏览器
渲染」，没意识到真正的变量是**会话 cookie**。上面每一次尝试都是在没有有效会话
cookie 的前提下做的。带上从成功的浏览器会话导出的 cookie 之后（实测）：

| 请求方式 | HTML 长度 | `/goto` 链接数 |
| --- | --- | --- |
| 带成功会话的 cookie | 1,266,169 | **368** |
| 不带 cookie（对照） | 92,147 | 0 |

结果页是服务端渲染的，标题、尺寸、站点名都在 HTML 里，文字结构和浏览器 DOM 一致。

「预热 cookie 无效」那一条也要修正：当时预热的是**首页** cookie，不是搜索成功页
的 cookie。这两者不等价。

### 3.2 「`GOOGLE_ABUSE_EXEMPTION` 是关键 cookie」—— 不成立

这个名字看起来就是"风控豁免"，而且只有 2.9 小时寿命，很容易当成关键因素。
逐个剔除的消融实验（实测，请求同一个 exact 页）：

| cookie 集合 | `/goto` | 判定 |
| --- | --- | --- |
| 全部 5 个 | 368 | 可用 |
| 去掉 `GOOGLE_ABUSE_EXEMPTION` | 368 | 可用 |
| 去掉 `AEC` | 365 | 可用 |
| 去掉 `DV` | 365 | 可用 |
| **去掉 `SOCS`** | 0 | 只有壳 |
| **去掉 `__Secure-ENID`** | 0 | 只有壳 |
| 只留 `GOOGLE_ABUSE_EXEMPTION` | 0 | 只有壳 |
| 任意单个 cookie | 0 | 只有壳 |
| `__Secure-ENID` + 自造 `SOCS` | 365 | **可用（最小集）** |

必需集是 `SOCS` + `__Secure-ENID` 两个。`SOCS` 是代码里写死的常量
（`config.SOCS_COOKIE`），所以真正要从浏览器换的只有 `__Secure-ENID`。

### 3.3 「动图抽帧能提高命中率」—— 不成立

写代码时想当然写进了 docstring：「原样上传 0 条，第一帧有结果」。实测两轮交替
对比，同一张 69 帧 GIF：

| 输入 | 第 1 轮 | 第 2 轮 | 首条结果 |
| --- | --- | --- | --- |
| GIF 原样上传 | 5 条 | 5 条 | Pinterest 那条 |
| 抽第一帧转 PNG | 5 条 | 5 条 | 完全一致 |

Google 自己取的就是第一帧（至少对这张图）。抽帧的价值是体积和确定性，
不是命中率。docstring 已按实测改写。

---

## 4. HTTP 快路径：已验证可行，尚未实现

拿到浏览器会话的 cookie 之后，整条链路都能走纯 HTTP。这部分**只做了验证，
没有落地到代码**，留档备用。

### 4.1 性能对比（实测）

| 路径 | 耗时 | 结果数 |
| --- | --- | --- |
| 现在的浏览器全流程 | 22~30s | 365 |
| 复用 cookie，纯 HTTP 只请求结果页 | 1.2~1.4s | 365~368 |
| 连上传也走 HTTP，全程无浏览器 | **3.1~4.4s** | 377~379 |

跨进程复用 cookie、浏览器进程已关闭的情况下连续 15 次：15/15，平均 3.4s，
最慢 3.7s。

除了快，还省掉常驻的无头 Chrome 进程（几百 MB 内存）。

### 4.2 `__Secure-ENID` 只能由浏览器换取（实测）

httpx 自己也能拿到 `__Secure-ENID`，但**不管用**：

| ENID 来源 | 长度 | 结果页 |
| --- | --- | --- |
| httpx 访问 google.com 首页 | 237 | 0 条，只有壳 |
| httpx 直接 POST `/v3/upload` | 218 | 0 条，只有壳 |
| httpx 拿的 ENID 复用到新会话 | 237 | 0 条，只有壳 |
| **浏览器会话** | **277** | **365 条可用** |

长度不同，浏览器那个明显携带额外信息。所以 ENID 不能自己造，也不能用 HTTP 换 ——
浏览器这一步省不掉，只能从"每次都用"降级成"偶尔用"。

### 4.3 cookie 能撑多久：**没有可信答案**

跑了 21 轮、每 3 分钟一次的长跑，同时对比"固定用最初 ENID"和"接受服务端刷新"：

* 0 ~ 53.1 分钟：两列全部可用，ENID 值从未变化
* 56.2 分钟：B 列（续期）失效
* 59.3 分钟：两列都失效

看起来像"约 55 分钟过期"，但**这个数据是脏的**：失效那一刻 A 列用的是同一个
未变的 ENID，两列同时失效，更像是出口 IP 被限流 —— 那一小时里这个实验自己就
发了 42 次请求，加上前面的实验总共 80 多次。

顺带发现另一个反直觉的点：profile 里 `AEC` 是 4 小时前创建的，而
`__Secure-ENID` 是 12 分钟前才重新下发的，和 12 分钟寿命的 `DV` 同一时刻刷新。
**所以 cookie 声明的 `expires`（ENID 是 396 天）完全不能当可用期看。**

要测准需要低频率长时间（比如每 15 分钟一次跑一整天），并且期间不做别的请求。
还没做。

### 4.4 实现要点（如果要做）

1. `__Secure-ENID` 落盘到插件数据目录，`SOCS` 用现有常量
2. HTTP 快路径：上传 + 结果页走 httpx，跳板还原已经是 httpx，不用改
3. 解析要从"页面里执行 JS"改成离线 HTML 解析，可复用 `parser.py` 的
   `_parse_lines()` 行结构逻辑。可行性已验证（极简 `HTMLParser` 能正确抽出
   标题 / 尺寸 / 站点名），但没做和 DOM 路径的逐条等价性比对
4. 失效检测必须扎实：拿到壳（HTML 约 92 KB、`/goto` 数为 0）、`Expired visual
   search`、`/sorry/` 都要能识别，然后回退浏览器重新换 cookie 并落盘
5. 不能假设 cookie 长期有效 —— 见 4.3

---

## 5. 诊断方法

### 5.1 按耗时判断

| 一次搜索耗时 | 含义 |
| --- | --- |
| 20~30s | 正常（浏览器路径） |
| 80s 以上且结果为 0 | 跳板还原全超时，检查代理是否对容器可达 |
| 3~4s | HTTP 快路径（当前代码不会出现） |

### 5.2 按 HTML 特征判断

| 特征 | 含义 |
| --- | --- |
| HTML ≈ 92 KB，`/goto` 数为 0 | 引导壳，会话 cookie 无效 |
| 含 `About 0 results` + `No matches for your search` | Google 确实没有匹配结果 |
| 可见文本含 `Expired visual search` | `vsrid` 会话失效，通常是上传方和浏览方身份不一致 |
| URL 落到 `/sorry/index` | 人机验证 |

注意 `Expired visual search` 这个字符串在正常页面的**隐藏节点**里也存在，
判断时要用 `inner_text`（只含可见文字），不能 grep 原始 HTML。这个坑踩过。

### 5.3 落盘看现场

`config.debug_dir` 设了之后，`browser.py::_dump()` 会存渲染后的 HTML 和截图。
只截可视区域 —— Lens 结果页整页截图能到几十 MB。

### 5.4 现成脚本

脚本清单见 [ARCHITECTURE.md 5.2](ARCHITECTURE.md#52-校验与排查脚本)。定位问题时
最常用的两个：`tools/diagnose.py` 分段报告哪一环坏了，`tools/check_browsers.py`
在换环境后跑一遍浏览器组合矩阵。

---

## 6. 在 Docker 容器里调试的操作备忘

这套环境有几个必踩的坑，记下来省时间。

**PowerShell 会吃掉命令里的 `$`，嵌套 heredoc 也会坏。** 可靠流程是：

```powershell
# 1. 本地写 .sh
# 2. 转成 LF
(Get-Content x.sh -Raw) -replace "`r`n","`n" | Set-Content -NoNewline x.lf.sh
# 3. 通过 stdin 送过去执行
Get-Content x.lf.sh -Raw | ssh <host> "cat > /tmp/x.sh && sh /tmp/x.sh"
```

**`docker exec` 要送 stdin 必须加 `-i`。** 漏了不会报错，heredoc 里的 Python
根本不执行，只是静默无输出。踩过两次。

**长命令的输出会在管道里丢。** 后台化 + 写日志文件：

```sh
nohup docker exec astrbot python3 /tmp/_x.py > /tmp/x.log 2>&1 &
# 之后 cat /tmp/x.log
```

**插件配置文件是 UTF-8 BOM**，读它必须 `encoding="utf-8-sig"`：
`/AstrBot/data/config/astrbot_plugin_image_search_config.json`

**改了插件文件不等于生效。** AstrBot 主进程里已导入的 Python 模块不会自动
替换，必须在 WebUI 重载插件或重启容器。判断办法：比对容器 stdout 里最后一次
`astrbot_plugin_image_search 已加载` 的时间戳和文件修改时间。

**实验完记得清理**，尤其是导出过 cookie 的文件 —— 那是真实会话凭证，不该留在
磁盘上。

---

## 7. 消息投递：两个只在 AstrBot 侧暴露的坑

搜索链路本身跑通之后，剩下的故障全出在「结果怎么发给用户」这一层。两个都不是
插件自己的 bug，而是没搞清 AstrBot pipeline 的行为。

### 7.1 `stop_event()` 会让后续 `yield` 全部作废

症状：**先发 `/搜图`、再补发图片**这条路，用户收到「正在搜索」之后就没有下文了。
另外两种触发方式（指令带图、引用图片）完全正常。

根因在 `_wait_for_image()` 的 `finally` 里对**原指令事件**调了 `stop_event()`。
它的本意是别让补图那条消息再去触发别的插件，但作用对象错了：

* `stop_event()` 把 `_force_stopped` 永久置位，没有对应的复位时机
* AstrBot 的 `scheduler` 是洋葱模型，每次 `yield` 前后都查 `is_stopped()`，
  为真就 `break`

于是搜索结果 `yield` 出去时，pipeline 已经不再往下走了 —— 消息根本没发出，也
不报错。

改法是把 `stop_event()` 移到 waiter 内部、作用在补图那条消息的事件上。原指令事件
必须保持可用，后面还要靠它发结果。

### 7.2 「时而合并转发、时而不合并」不是随机

症状：同一个指令，结果有时是一条合并转发，有时是普通长消息，偶尔还带引用。

根因是 `result_decorate` 阶段按全局配置装饰结果：

```python
if event.get_platform_name() == "aiocqhttp":
    word_cnt = sum(len(c.text) for c in result.chain if isinstance(c, Plain))
    if word_cnt > self.forward_threshold:        # platform_settings，默认 1500
        result.chain = [Node(name="AstrBot", content=[*result.chain])]
...
if self.reply_with_quote:
    result.chain.insert(0, Reply(id=event.message_obj.message_id))
```

搜图结果的字数随 AI 描述长短、结果条数、标题长短浮动，正好在 1500 上下来回，所以
看着像随机。引用则来自 `reply_with_quote`。

关键是**这些装饰只作用于 `result.chain`**，也就是 `yield` 或 `set_result()` 交出去
的结果。`event.send()` 走的是另一条路 —— 直接调平台适配器的
`send_group_msg` / `send_group_forward_msg`，整个装饰阶段都不经过。

所以改成全程 `event.send()` 之后，要不要合并转发完全由插件自己的配置决定，
行为变得确定。代价是 AstrBot 的全局文本转图片也不会作用于搜图结果 —— 对搜图来说
这反而是想要的：链接得能复制，变成图片就没用了。

顺带一提，`event.send()` 天然支持发多条消息，正好匹配「链接单独成条方便复制」
这个需求，不需要为了发 N 条消息去 `yield` N 次。

---

## 8. 未解决 / 待验证

| 项 | 状态 | 影响 |
| --- | --- | --- |
| HTTP 快路径未落地 | 可行性已验证，代码没写 | 性能：22~30s vs 3~4s |
| `__Secure-ENID` 真实寿命 | 长跑数据被自己的请求量污染，无可信结论 | 决定快路径的实用价值 |
| 换代理节点后旧 cookie 是否还有效 | 没测 | 快路径的失效检测策略 |
| 离线 HTML 解析与 DOM 解析的等价性 | 只验证了"能抽出正确字段"，没逐条比对 | 快路径的结果质量 |
| Windows 上自动安装的 CfT 151 启动退出码 3 | 已知，未修 | 只影响 Windows 本地跑自动安装；装了系统 Chrome 会优先命中所以平时走不到 |
| 本机 `verify_parser.py` 失败 | 环境问题：playwright 升级后没跑 `playwright install`，缺 `chromium_headless_shell` | 只影响本地这一个校验脚本 |
| 标题被服务端截断 | Google 行为，无法绕过 | 见下 |

### 标题截断是服务端行为，加宽窗口无用

Google 在结果页**服务端**就把过长标题截成 `xxx ...`，完整标题不在 HTML 里。
实测 1920 / 2560 / 3440 三档窗口宽度，截断位置完全一致，30 条结果里 16 条被截断。

所以 `titles.py` 只能去抓目标页的 `<title>` 来补，属于尽力而为：实测三个站点里
两个被 Cloudflare 拦（403 / 503）。只有当目标页标题以截断前缀开头时才替换 ——
否则说明跳到了首页或反爬页，保留原值更安全。

### 请求量会招来人机验证

实测一小时内约 80 次搜索之后，同一出口 IP 开始连续弹 `/sorry/index`，隔十分钟
左右自行恢复。正常使用配合默认的用户冷却不会触到，但压测和批量脚本很容易踩。
做实验时要留意：**连续失败先怀疑自己的请求量，不要急着归因到代码改动上。**
