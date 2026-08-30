"""解析 Google Lens "Exact matches" 结果页。

页面结构（2026-08 实测）：

* 每条结果是一个 ``<a href="/goto?url=<不透明编码>">``，**真实地址不在页面里** ——
  整个 HTML 里搜不到任何结果站点的域名，也没有 ``AF_initDataCallback`` 数据块。
  唯一的还原办法是请求这个 ``/goto`` 地址，读 302 的 ``Location``。
* 链接的可见文字是固定行结构::

      标题
      [日期]
      [·]
      宽x高          # 带千分位逗号，如 1,200x1,684
      站点名          # 如 Amazon.jp / 駿河屋 / らしんばんオンライン

  标题过长时 Google 会在服务端截断并加 ``...``，页面里拿不到完整标题。

所以解析分两步：先在页面里抽出「``/goto`` 地址 + 文字行 + 缩略图」，
再由调用方（:mod:`image_search.searcher`）把 ``/goto`` 批量解析成真实地址。

Google 的 class 名是哈希且会轮换的，所以这里不依赖任何 class，只依赖
「跳板链接 + 文字行」这个结构。
"""

from __future__ import annotations

import dataclasses
import re
import urllib.parse as up
from typing import Any

from .logger import logger
from .models import ExactMatch

# 在页面上下文里执行，抽出候选结果卡片
EXTRACT_SCRIPT = r"""
() => {
  const GOOGLE_HOST = /(^|\.)(google|gstatic|googleusercontent|googleapis|googleadservices|youtube|blogger)\.[a-z.]{2,}$/i;

  // 结果链接有三种可能形态，都要认：
  //   /goto?url=<编码>   Lens 结果页当前用的跳板
  //   /url?q=<真实地址>   老式跳板
  //   https://站外/...    偶尔直接给真实地址
  function classify(a) {
    const raw = a.getAttribute('href') || '';
    if (raw.startsWith('/goto?') || raw.startsWith('/url?') ||
        raw.startsWith('/imgres?')) {
      let u;
      try { u = new URL(raw, location.href); } catch (e) { return null; }
      const img = u.searchParams.get('imgurl');
      for (const key of ['imgrefurl', 'url', 'q']) {
        const v = u.searchParams.get(key);
        if (v && /^https?:\/\//i.test(v)) return { url: v, goto: null, imageUrl: img };
      }
      return { url: null, goto: u.href, imageUrl: img };
    }
    let u;
    try { u = new URL(raw, location.href); } catch (e) { return null; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    if (GOOGLE_HOST.test(u.hostname)) return null;
    return { url: u.href, goto: null, imageUrl: null };
  }

  function thumbOf(el) {
    for (const img of el.querySelectorAll('img')) {
      const src = img.currentSrc || img.src || '';
      if (!src || src.startsWith('data:image/gif')) continue;
      if (img.naturalWidth && img.naturalWidth <= 2) continue;
      return src;
    }
    return null;
  }

  const seen = new Set();
  const out = [];

  for (const a of document.querySelectorAll('a[href]')) {
    const target = classify(a);
    if (!target) continue;

    const key = target.goto || target.url;
    if (seen.has(key)) continue;

    // 卡片容器：优先用链接自身；文字不在链接里时再往上找一层
    let card = a;
    if (!(a.innerText || '').trim()) {
      let cur = a;
      for (let i = 0; i < 3 && cur.parentElement; i++) {
        cur = cur.parentElement;
        if ((cur.innerText || '').trim() && cur.querySelector('img')) {
          card = cur;
          break;
        }
      }
    }
    const text = (card.innerText || '').trim();
    const aria = a.getAttribute('aria-label') || a.getAttribute('title') || '';
    if (!text && !aria) continue;

    seen.add(key);
    out.push({
      url: target.url,
      goto: target.goto,
      imageUrl: target.imageUrl || null,
      lines: text.split('\n').map(s => s.trim()).filter(Boolean).slice(0, 10),
      ariaLabel: aria || null,
      thumbnail: thumbOf(a) || thumbOf(card),
      hasImg: !!card.querySelector('img'),
    });
  }
  return { items: out, pageTitle: document.title, url: location.href };
}
"""

# 宽x高，可能带千分位逗号：739x1,000 / 1,200×1,684
# 在 AI 模式（udm=50）页面上执行，把 AI 回答那棵子树整块搬回来。
#
# 这个脚本只做两件事：划定子树范围、给不渲染的元素打标记。文本怎么拼、
# 哪些该丢，全部交给 :func:`ai_html_to_text` 用 bs4 按结构判断 ——
# 页面脚本里不方便写复杂逻辑，也没法离线测试。
#
# 结构（2026-08 实测）：
#   * AI 回答正文的首个片段带 ``data-subtree="aimfl"``，可以用它判断回答
#     是否已经开始生成
#   * 整块回答在 ``[data-subtree="aimc"]`` 里，主内容列是其中的
#     ``[data-container-id="main-col"]``
AI_EXTRACT_SCRIPT = r"""
() => {
  const anchor = document.querySelector('[data-subtree="aimfl"]');
  const answer = document.querySelector('[data-subtree="aimc"]');
  // 内容根的优先级：
  //   1. 回答区里的 [data-container-id="main-col"] —— 主内容列，最干净
  //   2. [data-subtree="aimc"] —— 整块回答，可能连底部引用卡片一起
  //   3. 首句所在的 [data-container-id] 容器
  let root = answer ? answer.querySelector('[data-container-id="main-col"]')
                    : null;
  if (!root) root = answer;
  if (!root && anchor) root = anchor.closest('[data-container-id]');
  if (!root) return {started: false, html: '', charCount: 0};

  // 可见性只有浏览器知道，HTML 里看不出来。这里给真正不渲染的元素打个标记，
  // 交给 Python 侧按标记删除。只认 display:none / visibility:hidden ——
  // display:contents 的容器自身没有盒子，但内容是可见的，不能算隐藏。
  for (const el of root.querySelectorAll('*')) {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') {
      el.setAttribute('data-is-hidden', '1');
    }
  }

  return {started: !!anchor, html: root.outerHTML,
          charCount: (root.innerText || '').trim().length};
}
"""

# 按结构删除的选择器。全部是「这一类元素本身就不是正文」，
# 和 AI 说了什么无关，所以不会因为 Google 换措辞而失效：
#   data-is-hidden  提取脚本标记的不渲染元素（隐藏的标题、分享面板、反馈提示）
#   button          引用来源胶囊，实测是 <button aria-label="相关结果">
#                   或 <button aria-label="巴哈姆特（另有 6 个）- …">
#   aria-hidden     纯装饰节点
_AI_DROP_SELECTORS = (
    "[data-is-hidden]",
    "button",
    '[aria-hidden="true"]',
    "svg", "img", "picture", "style", "script", "noscript", "template",
)
# 块级标签：渲染成纯文本时各占一行
_AI_BLOCK_TAGS = {
    "div", "p", "li", "ul", "ol", "tr", "table", "section", "article",
    "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr", "dt", "dd",
}
# AI 拒绝识别时的说法。这类回答没有信息量，当作没有描述处理，
# 免得输出里只剩一句「抱歉」。这是唯一还靠措辞判断的地方 ——
# 因为「拒答」本身就只能从语义上认出来。
_AI_REFUSAL_RE = re.compile(
    r"(抱歉[，,]?\s*我无法|我无法(为您)?提供|我不能帮|无法(进行)?识别"
    r"|无法为您提供|不(可能|方便)提供|i (can'?t|cannot) help"
    r"|i'?m (not able|unable)|can'?t provide"
    r"|unable to (help|provide|identify))",
    re.IGNORECASE)

_DIMENSION_RE = re.compile(r"^(\d[\d,]*)\s*[x×X✕*]\s*(\d[\d,]*)$")
# 日期行：Jul 23, 2019 / 2019年7月23日 / Aug 10, 2026
_DATE_RE = re.compile(
    r"^(?:\d{4}年\d{1,2}月\d{1,2}日"
    r"|[A-Z][a-z]{2}\s+\d{1,2},\s*\d{4}"
    r"|\d{4}-\d{2}-\d{2})$")
_SEPARATOR_LINES = {"·", "-", "|", "—", "•"}
_NOISE_LINES = {
    "translate this page", "visit", "shop", "more", "similar", "feedback",
    "about this image", "exact matches", "visual matches", "all", "sign in",
    "翻译此页", "翻訳", "このページを訳す",
}


@dataclasses.dataclass(slots=True)
class RawMatch:
    """页面上抽出来的一条候选结果，``url`` 可能还没还原。

    Attributes:
        url: 真实地址，若只有跳板则为 None。
        goto: ``/goto?url=...`` 跳板地址，需要跟随 302 才能拿到真实地址。
    """

    url: str | None
    goto: str | None
    content: str
    source: str | None = None
    thumbnail: str | None = None
    image_url: str | None = None
    date: str | None = None
    width: int | None = None
    height: int | None = None

    def to_exact_match(self, url: str) -> ExactMatch:
        return ExactMatch(
            url=url,
            content=self.content,
            source=self.source,
            thumbnail=self.thumbnail,
            image_url=self.image_url,
            width=self.width,
            height=self.height,
            date=self.date,
        )


def _to_int(text: str) -> int | None:
    try:
        return int(text.replace(",", "").replace("，", ""))
    except ValueError:
        return None


def _parse_lines(lines: list[str], aria: str | None) -> dict[str, Any]:
    """按 ``标题 / 日期 / 尺寸 / 站点名`` 的行结构拆解卡片文字。"""
    width = height = None
    date = None
    kept: list[str] = []

    for line in lines:
        if line in _SEPARATOR_LINES:
            continue
        if line.lower() in _NOISE_LINES:
            continue
        dim = _DIMENSION_RE.match(line)
        if dim:
            width, height = _to_int(dim.group(1)), _to_int(dim.group(2))
            continue
        if _DATE_RE.match(line):
            date = line
            continue
        kept.append(line)

    title = ""
    source = None
    label = (aria or "").strip()
    if len(kept) >= 2:
        # 第一行是标题，最后一行是站点名
        title, source = kept[0], kept[-1]
    elif kept:
        # 只有一行时，若 aria-label 另有内容，那一行更可能是站点名
        if label and label != kept[0]:
            title, source = label, kept[0]
        else:
            title = kept[0]

    if not title and label:
        title = label
    # 标题和站点名相同说明这张卡片没有独立标题，用 aria-label 兜底
    if title and source and title == source and label:
        title = label

    return {"content": title, "source": source, "date": date,
            "width": width, "height": height}


def _cells_to_line(cells: list[str]) -> str:
    """表格一行转成一行文本。两列就是「字段：值」，多列用竖线隔开。"""
    kept = [c for c in cells if c]
    if not kept:
        return ""
    if len(kept) == 2:
        return f"{kept[0]}：{kept[1]}"
    return " | ".join(kept)


# 「相关内容」网格的判定门槛。正文段落最多带一个内联链接和一个引用胶囊
# 图标（各 1 个），网格实测是 60 张图 / 30 个链接 —— 差着一个数量级，
# 所以门槛取多少都无所谓，这里留足余量。
_GRID_MIN_IMAGES = 4
_GRID_MIN_LINKS = 4


def _content_layer(root: Any) -> Any:
    """向下钻到「内容块层」：正文段落彼此为兄弟的那一层。

    ``main-col`` 到正文之间套着几层单子元素的包装 div，层数不固定，
    所以按「子元素带文字的超过一个」来判断已经到底。
    """
    node = root
    for _ in range(12):
        kids = [c for c in node.find_all(recursive=False)
                if c.get_text(strip=True)]
        if len(kids) != 1:
            return node
        node = kids[0]
    return node


def _truncate_at_related_grid(root: Any) -> bool:
    """砍掉「相关内容」图片网格，以及它后面的一切。

    AI 回答的版面顺序是固定的：正文段落 → 相关内容网格 → 追问建议 →
    分享／反馈面板。这几段用的属性是同一套（都带 ``data-hveid``），
    class 又是轮换的哈希，所以唯一稳的分界就是网格本身的形态 ——
    一个块里塞进几十张缩略图和外链，正文段落做不到这件事。

    命中网格就把它连同后面的兄弟全部删掉，追问建议和分享面板一起没了，
    不需要再去猜哪句话是引导语。

    网格**不是每次都出现** —— 同一张图多次搜索，有时给网格有时不给。没有
    网格时这里什么都不做，追问建议会留在输出里。这是权衡后的选择：追问和
    正文在结构上确实分不开（两者属性同为 ``data-hveid``、``data-sae``，
    嵌套层级也会互换），硬要区分只能回去赌措辞，而赌错的代价是砍掉正文。
    多几行说明文字用户一眼能忽略，少了正文却看不出来。

    注意必须在删 ``img``/``[data-is-hidden]`` **之前**调用：网格里的图和
    文字大半带隐藏标记，删完之后它就是个空 div，特征全没了。

    Returns:
        是否找到并截断了网格。
    """
    layer = _content_layer(root)
    for block in layer.find_all(recursive=False):
        if (len(block.find_all("img")) >= _GRID_MIN_IMAGES
                and len(block.find_all("a")) >= _GRID_MIN_LINKS):
            for tail in list(block.find_next_siblings()):
                tail.decompose()
            block.decompose()
            return True
    return False


_SENTENCE_END = ("。", "！", "!", "？", "?", "；", ";", ".")
# 句子边界：中文句读，以及后面跟空白的英文句点
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])|(?<=\.)(?=\s)")
_QUESTION_MARKS = ("？", "?")


def _drop_trailing_questions(lines: list[str]) -> None:
    """丢掉结尾的问句。

    AI 习惯在回答末尾追一句「你想进一步了解哪方面的内容呢？」这类邀请继续
    对话的提问，对搜图没有意义。判据只有位置和标点：从最后一行往前，末句以
    问号收尾就丢掉，遇到不是问句的立刻停。

    这么定是因为追问和正文在 DOM 结构上确实分不开（见 DESIGN_NOTES 2.10），
    而按措辞去匹配又太脆弱 —— 回答语言由 Google 按出口 IP 判定，换个节点就
    从中文变英文，话术也跟着换。问号是标点，不是措辞：描述一张图片的正文是
    客观陈述，不会以问号结尾，所以这条规则对语言和说法都不敏感。

    代价是句号或感叹号收尾的邀请语（「请告诉我你接下来想了解的内容。」）留
    得住。宁可多留一句，也不去赌措辞把正文删掉。

    一行里可能前半是正文、后半才是提问，所以按句切开处理，只砍末尾那几句。
    """
    while lines:
        sentences = [s for s in _SENTENCE_SPLIT_RE.split(lines[-1]) if s]
        dropped = False
        while sentences and sentences[-1].rstrip().endswith(_QUESTION_MARKS):
            sentences.pop()
            dropped = True
        if not dropped:
            break
        rest = "".join(sentences).strip()
        if rest:
            # 这一行还剩正文，砍到这里为止
            lines[-1] = rest
            break
        lines.pop()


def _trim_dangling_tail(lines: list[str]) -> None:
    """把结尾悬空的引导句砍掉。

    正文最后一段常以冒号收尾去引出下面的网格（「以下是更多相关内容：」）。
    网格已经被 :func:`_truncate_at_related_grid` 删了，这个冒号就悬着。
    能退回同一行里上一个句末标点就只砍这半句，否则整行丢掉。

    只处理冒号，是因为它在标点上就代表「后面还有东西」，和 AI 怎么措辞无关。
    """
    while lines and lines[-1].endswith(("：", ":")):
        tail = lines[-1]
        for mark in _SENTENCE_END:
            index = tail.rfind(mark)
            if index > 0:
                lines[-1] = tail[: index + len(mark)]
                return
        lines.pop()


def ai_html_to_text(html: str, max_chars: int = 900) -> str:
    """把 AI 回答的 HTML 片段按结构还原成纯文本。

    做法完全依赖 DOM 结构，不去猜 AI 说了什么：

    * 先在「相关内容」图片网格处截断，网格连同后面的追问建议、分享面板
      一起丢掉（见 :func:`_truncate_at_related_grid`）
    * 再按选择器删掉「本来就不是正文」的元素 —— 提取脚本标记的不渲染元素、
      引用来源胶囊（``<button>``）、装饰节点（``aria-hidden``）、图片
    * 剩下的按标签语义转文本：``role="heading"`` 和 ``h1``~``h6`` 当小标题，
      ``li`` 加项目符号，``table`` 的行转成「字段：值」，其余块级元素各占一行

    这样 Google 换措辞、换语言都不影响解析，只有改动 DOM 结构才需要跟进。
    万一版面变了、网格认不出来，结果是多输出几行追问建议，而不是把正文
    截断或者丢掉 —— 宁可多给，不能少给。

    AI 对部分图片会拒答，而且同一张图不是每次都拒。拒答文案
    没有信息量，留着只会在结果里多出一句「抱歉」，所以整段丢掉，让输出只保留
    完全匹配那部分。
    """
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        logger.warning("未安装 beautifulsoup4，无法解析 AI 描述")
        return ""

    soup = BeautifulSoup(html, "html.parser")
    root = soup.body or soup
    # 顺序要紧：网格靠「一堆 img + 一堆 a」认出来，删噪会把这些特征抹掉
    if not _truncate_at_related_grid(root):
        logger.debug("AI 回答里没找到相关内容网格，按原样输出")
    for selector in _AI_DROP_SELECTORS:
        for node in root.select(selector):
            node.decompose()

    # 表格先整体转成文本行，免得单元格被拆成一堆孤立短句
    for table in root.find_all("table"):
        rows = [_cells_to_line([" ".join(cell.get_text(" ", strip=True).split())
                                for cell in row.find_all(["th", "td"])])
                for row in table.find_all("tr")]
        table.replace_with("\n" + "\n".join(r for r in rows if r) + "\n")

    # 列表里夹着占位用的空 li（实测每个 ul 首尾各一个），加了符号就会在
    # 输出里留下一个孤零零的「•」
    for item in root.find_all("li"):
        if item.get_text(strip=True):
            item.insert(0, "• ")
    # 小标题前后留空行，读起来有层次
    for heading in root.find_all(
            lambda tag: tag.name in {"h1", "h2", "h3", "h4", "h5", "h6"}
            or tag.get("role") == "heading"):
        heading.insert_before("\n")
        heading.insert_after("\n")
    for tag in root.find_all(lambda t: t.name in _AI_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")

    lines: list[str] = []
    seen: set[str] = set()
    # 分隔符必须是空串：用 "\n" 会在每个文本节点之间都插换行，句子会被
    # <strong> 之类的内联标签切碎。换行只来自上面显式插入的那些。
    for raw in root.get_text("").splitlines():
        text = " ".join(raw.split())
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append(text)

    # 先剔结尾问句，再处理冒号断尾 —— 删掉问句可能露出新的断尾引导语
    _drop_trailing_questions(lines)
    _trim_dangling_tail(lines)
    summary = "\n".join(lines).strip()
    if summary and _AI_REFUSAL_RE.search(summary) and len(summary) < 200:
        return ""
    if len(summary) > max_chars:
        # 尽量在句子边界断开，读起来不至于半句话没了
        cut = summary[:max_chars]
        for mark in ("。", "\n", ". "):
            index = cut.rfind(mark)
            if index > max_chars * 0.6:
                cut = cut[: index + len(mark)]
                break
        summary = cut.rstrip() + "…"
    return summary


def extract_items(payload: dict[str, Any], max_results: int = 20) -> list[RawMatch]:
    """把页面脚本的返回值整理成 :class:`RawMatch` 列表。"""
    results: list[RawMatch] = []
    for item in payload.get("items") or []:
        url = item.get("url")
        goto = item.get("goto")
        if not url and not goto:
            continue
        fields = _parse_lines([str(x) for x in item.get("lines") or []],
                             item.get("ariaLabel"))
        if not fields["content"]:
            continue
        # 结果卡片一定带缩略图；没有缩略图且文字很短的基本是导航链接
        if not item.get("hasImg") and len(fields["content"]) < 8:
            continue
        results.append(RawMatch(
            url=url, goto=goto, thumbnail=item.get("thumbnail"),
            image_url=item.get("imageUrl"), **fields))
        if len(results) >= max_results:
            break
    return results


def parse_extracted(payload: dict[str, Any], max_results: int = 20) -> list[ExactMatch]:
    """只用页面里已有的信息构造结果（跳板链接会被跳过）。

    主要给离线测试用；正常流程走 :func:`extract_items` 再解析跳板。
    """
    out: list[ExactMatch] = []
    for raw in extract_items(payload, max_results):
        target = raw.url or _try_unwrap(raw.goto)
        if target:
            out.append(raw.to_exact_match(target))
    return out


def _try_unwrap(goto: str | None) -> str | None:
    """跳板地址里若明文带着真实地址就直接取出来。"""
    if not goto:
        return None
    query = dict(up.parse_qsl(up.urlsplit(goto).query))
    for key in ("imgrefurl", "url", "q"):
        value = query.get(key, "")
        if value.startswith(("http://", "https://")):
            return value
    return None
