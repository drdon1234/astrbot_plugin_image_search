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
# 在 AI 模式（udm=50）页面上执行，抽出 AI 给的图片描述。
#
# 结构（2026-08 实测）：
#   * AI 回答正文的首个片段带 ``data-subtree="aimfl"``，可以用它判断回答
#     是否已经开始生成
#   * 正文各段都**不在** ``<a>`` 里
#   * 下方的引用来源卡片全部包在 ``<a>`` 里
#   * 追问建议（「我们可以聊聊：」后面那几条）是可点击的，也在 ``<a>`` 里
#
# 所以"取不在 <a> 内的叶子文本块"就能把正文和其余部分分开，不依赖 class
# （Google 的 class 是哈希且会轮换的）。
AI_EXTRACT_SCRIPT = r"""
() => {
  const anchor = document.querySelector('[data-subtree="aimfl"]');
  // 容器优先级很关键：
  //   * 首句所在的 [data-container-id] 容器 = 正文（含追问建议），不含底部卡片
  //   * [data-subtree="aimc"] 是整块回答，**连引用卡片一起**，只能兜底
  // 实测前者 3032 字、后者 3442 字，差值就是那堆卡片。
  let box = anchor ? anchor.closest('[data-container-id]') : null;
  if (!box) box = document.querySelector('[data-subtree="aimc"]');
  if (!box && anchor) box = anchor.parentElement;
  if (!box) return {started: false, blocks: [], drop: [], charCount: 0};

  // 直接取容器的 innerText，不逐个元素抓：
  //   * 句子里内联的链接（作品名、系列名）会留在句中，不会被拆成独立短行
  //   * 隐藏内容（分享面板、反馈提示）本来就不算进 innerText
  //   * 表格单元格之间是 \t，换成「：」正好是「字段：值」
  const text = (box.innerText || '').trim();
  const blocks = text.split('\n')
      .map((line) => line.replace(/\t+/g, '：').trim())
      .filter(Boolean);

  // 收集所有链接的文字。调用方只在「一整行恰好等于某个链接文字」时才丢掉，
  // 这样独占一行的引用标记（来源胶囊）和追问建议会被剔除，
  // 而内联在句子里的链接（作品名）所在的行更长、不会误伤。
  const drop = [];
  for (const link of box.querySelectorAll('a')) {
    const linkText = (link.innerText || '').replace(/\t+/g, '：').trim();
    if (linkText) drop.push(linkText);
  }

  return {started: !!anchor, blocks: blocks,
          drop: Array.from(new Set(drop)), charCount: text.length};
}
"""

# 正文之后的东西，遇到就停：追问建议、展开按钮、分享面板
_AI_STOP_RE = re.compile(
    r"(我们可以聊聊|如果[你您](对|想)|想进一步了解|要不要我|需要我帮[你您]"
    r"|可以为[你您](提供|介绍)|如需(了解|查询|获取|查看)|建议[你您]"
    r"|[你您]可以(浏览|参考|查看)|以下(精选|相关)"
    r"|explore (similar|more)|if you('re| are) interested"
    r"|want to (know|learn) more|related searches?|you (can|may) (also )?browse"
    r"|^see (more|less)$|^显示(更多|更少)$|^展开$|^收起$"
    r"|分享公开链接|此公开链接在|无法复制链接|复制链接"
    r"|^(facebook|twitter|whatsapp|reddit|电子邮件|嵌入)$)",
    re.IGNORECASE)
# 页面框架上的固定文案。有 aimfl 锚点时这些基本已被切掉，
# 这里作为拿不到锚点时的兜底
_AI_NOISE = {
    "ai mode", "all", "exact matches", "visual matches", "images", "videos",
    "shopping", "web", "news", "search", "sign in", "feedback",
    "send feedback", "settings", "quick settings", "google apps", "privacy",
    "terms", "help", "tools", "safesearch", "安全搜索", "登录", "设置",
    "反馈", "隐私权", "条款", "帮助", "工具", "全部", "图片", "视频",
    "更多", "搜索", "ai 模式", "完全匹配", "视觉匹配", "ai 模式对话",
    "ai 模式历史记录", "您已退出账号", "跳到主要内容", "无障碍功能帮助",
    "相似插画与周边视觉", "see more", "see less",
}
_AI_NOISE_RE = re.compile(
    r"^(若要访问历史记录|要访问历史记录|管理 AI 模式|AI 模式(历史记录|对话)"
    r"|您发送了|您已退出|AI 模式针对|AI Mode responded"
    r"|AI-generated|AI 生成|以上内容由 AI|Google 搜索|Google Search"
    r"|按 /|Press /|Ctrl\+|结果数|About [\d,]+ results|找到约"
    r"|跳到主要内容|无障碍功能)",
    re.IGNORECASE)
# 引用卡片的行特征。容器边界万一没框住卡片（Google 改版），靠这些兜底：
#   * 摘要行以日期开头，后面跟破折号
#   * 来源行是个裸域名
#   * 「全部显示」是展开整组卡片的按钮
_AI_CARD_RE = re.compile(
    r"(^\d{4}年\d{1,2}月\d{1,2}日\s*[—–-]"
    r"|^\d{1,2}\s+\w{3,9}\s+\d{4}\s*[—–-]"
    r"|^[\w-]+(\.[\w-]+){1,3}\s*$"
    r"|^(全部显示|显示全部|show all|view all)"
    r"|·\s*\d+\s*(年|个?月|天|小时|分钟)前\s*$"
    r"|·\s*\d+\s*(year|month|week|day|hour|minute)s?\s+ago\s*$)",
    re.IGNORECASE)
# 用来判断一行「像不像正文」：有句读或是「字段：值」形式就算正文
_AI_SENTENCE_RE = re.compile(r"[。！？；，、：:.!?]")
# AI 拒绝识别时的说法。这类回答没有信息量，当作没有描述处理，
# 免得输出里只剩一句「抱歉」
_AI_REFUSAL_RE = re.compile(
    r"(抱歉[，,]?\s*我无法|我无法(为您)?提供|我不能帮|无法(进行)?识别"
    r"|无法为您提供|i (can'?t|cannot) help|i'?m (not able|unable)"
    r"|can'?t provide|unable to (help|provide|identify))",
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


def clean_ai_summary(payload: dict[str, Any], max_chars: int = 900) -> str:
    """把 :data:`AI_EXTRACT_SCRIPT` 的结果整理成一段可直接发送的描述。

    正文范围由 :data:`AI_EXTRACT_SCRIPT` 划定（回答区开头到第一张卡片之前），
    这里只做行级清理：剔除脚本标出的独立链接行（追问建议、来源标记）、
    滤掉残留的框架文案、把「AI 拒绝识别」当成没有描述、限制总长度免得刷屏。

    AI 对部分图片会拒答，而且同一张图不是每次都拒 ——
    拒答文案没有信息量，留着只会在结果里多出一句「抱歉」，所以直接丢掉，
    让输出只保留完全匹配那部分。
    """
    drop = {" ".join(str(item).split())
            for item in (payload.get("drop") or [])}
    lines: list[str] = []
    seen: set[str] = set()
    for raw in payload.get("blocks") or []:
        text = " ".join(str(raw).split())
        if not text or text in drop or text in seen:
            continue
        if text.lower() in _AI_NOISE:
            continue
        if _AI_NOISE_RE.match(text):
            continue
        # 卡片区一旦开始，后面就全是卡片了，直接收尾
        if _AI_CARD_RE.search(text) or _AI_STOP_RE.search(text):
            break
        if len(text) < 4:
            continue
        seen.add(text)
        lines.append(text)

    # 收尾：削掉末尾那些「短且没有标点」的行。卡片区总在最后，来源站点名
    # （手机新浪网、Pinterest 之类）就长这样；正文的小标题不会落在最后一行，
    # 而「字段：值」形式带冒号，不会被误删。
    while lines:
        tail = lines[-1]
        if len(tail) <= 24 and not _AI_SENTENCE_RE.search(tail):
            lines.pop()
            continue
        break

    summary = "\n".join(lines).strip()
    if summary and _AI_REFUSAL_RE.search(summary) and len(summary) < 200:
        return ""
    if len(summary) > max_chars:
        # 尽量在句子边界断开，读起来不至于半句话没了
        cut = summary[:max_chars]
        for mark in ("。", "\n", ". "):
            index = cut.rfind(mark)
            if index > max_chars * 0.6:
                cut = cut[:index + len(mark)]
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
