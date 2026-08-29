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
