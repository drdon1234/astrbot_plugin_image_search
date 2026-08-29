"""离线校验解析器：构造一个模仿 Lens "Exact matches" 卡片结构的页面，
在真实 Chromium 里跑一遍提取脚本 + Python 归一化，检查输出是否符合预期。

覆盖四种链接形态和真实的行结构（标题 / 日期 / 千分位尺寸 / 站点名），
不需要联网访问 Google。

    python tools/verify_parser.py
"""

from __future__ import annotations

import asyncio
import base64
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from image_search.parser import (  # noqa: E402
    EXTRACT_SCRIPT, extract_items, parse_extracted,
)

OPAQUE = "CAESlwMB6zswFfR2p_k-svuFzpq86zKSJ_c-IOLcDrrsDVYRRMWOk9jygn4NTUVy"


def small_png_data_uri() -> str:
    """造一张 8x8 的 PNG，避免被 1x1 占位图过滤掉。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (120, 160, 200)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def build_mock_page(thumb: str) -> str:
    """卡片结构照实测形态写：链接里包着缩略图 + 标题 / 日期 / 尺寸 / 站点名。"""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Exact matches</title></head>
<body>
  <!-- 顶部导航：全是 google 域名，应被过滤 -->
  <nav>
    <a href="https://www.google.com/search?udm=26">All</a>
    <a href="https://www.google.com/search?udm=48">Exact matches</a>
  </nav>

  <div id="results">
    <!-- 1. 站外直链 + 千分位尺寸 -->
    <div class="card">
      <a href="https://shop.lashinbang.com/products/detail/2273450">
        <div><img src="{thumb}" alt=""></div>
        <div>
          <div>BUNNY A GIRL! 【青春ブタ野郎 シリーズ】[溝口ケージ][NtyPe]</div>
          <div>1,280x1,796</div>
          <div>らしんばんオンライン</div>
        </div>
      </a>
    </div>

    <!-- 2. /url?q= 跳板，带日期行和分隔符行 -->
    <div class="card">
      <a href="/url?q=https%3A%2F%2Fwww.melonbooks.co.jp%2Fdetail%2Fdetail.php%3Fproduct_id%3D123456&amp;sa=U">
        <div><img src="{thumb}" alt=""></div>
        <div>
          <div>BUNNY A GIRL! / NtyPe - メロンブックス</div>
          <div>Jul 23, 2019</div>
          <div>·</div>
          <div>639x900</div>
          <div>Melonbooks</div>
        </div>
      </a>
    </div>

    <!-- 3. /imgres 形态：imgrefurl 是页面，imgurl 是原图 -->
    <div class="card">
      <a href="/imgres?imgurl=https%3A%2F%2Fimg.example-cdn.net%2Fbunny.jpg&amp;imgrefurl=https%3A%2F%2Fwww.pixiv.net%2Fartworks%2F71234567">
        <div><img src="{thumb}" alt=""></div>
        <div>
          <div>Bunny A Girl! - pixiv</div>
          <div>800x1,120</div>
          <div>pixiv</div>
        </div>
      </a>
    </div>

    <!-- 4. 标题只在 aria-label 上，可见文字只有站点名 -->
    <div class="card">
      <a href="https://www.suruga-ya.jp/product/detail/ZHORI0987"
         aria-label="BUNNY A GIRL! NtyPe 同人誌 - 駿河屋">
        <div><img src="{thumb}" alt=""></div>
        <div><div>駿河屋</div></div>
      </a>
    </div>

    <!-- 5. /goto 不透明跳板：真实地址不在页面里，只能靠跟随 302 还原 -->
    <div class="card">
      <a href="/goto?url={OPAQUE}">
        <div><img src="{thumb}" alt=""></div>
        <div>
          <div>コミケ NtyPe 溝口ケージ 会場限定本 「BUNNY A GIRL!」青春ブタ ...</div>
          <div>739x1,000</div>
          <div>Amazon.jp</div>
        </div>
      </a>
    </div>
  </div>

  <!-- 页脚：google 域名 + 无图短文本，都该被过滤 -->
  <footer>
    <a href="https://policies.google.com/privacy">Privacy</a>
    <a href="https://example.com/x">Ad</a>
  </footer>
</body></html>
"""


# (url, goto 是否为空, content, source, width, height, date)
EXPECTED = [
    ("https://shop.lashinbang.com/products/detail/2273450", True,
     "BUNNY A GIRL! 【青春ブタ野郎 シリーズ】[溝口ケージ][NtyPe]",
     "らしんばんオンライン", 1280, 1796, None),
    ("https://www.melonbooks.co.jp/detail/detail.php?product_id=123456", True,
     "BUNNY A GIRL! / NtyPe - メロンブックス", "Melonbooks", 639, 900, "Jul 23, 2019"),
    ("https://www.pixiv.net/artworks/71234567", True,
     "Bunny A Girl! - pixiv", "pixiv", 800, 1120, None),
    ("https://www.suruga-ya.jp/product/detail/ZHORI0987", True,
     "BUNNY A GIRL! NtyPe 同人誌 - 駿河屋", "駿河屋", None, None, None),
    (None, False,
     "コミケ NtyPe 溝口ケージ 会場限定本 「BUNNY A GIRL!」青春ブタ ...",
     "Amazon.jp", 739, 1000, None),
]


async def main() -> int:
    html = build_mock_page(small_png_data_uri())

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        async def handler(route):
            await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                body=html)

        # 伪造成真实结果页地址，顺带验证相对跳板链接的还原
        await page.route("https://www.google.com/search*", handler)
        await page.goto("https://www.google.com/search?udm=48",
                        wait_until="domcontentloaded")
        payload = await page.evaluate(EXTRACT_SCRIPT)
        await browser.close()

    items = extract_items(payload, max_results=20)
    print(f"页面脚本抓到候选 {len(payload['items'])} 个 -> 归一化 {len(items)} 条\n")
    for it in items:
        print(f"  url:     {it.url}")
        print(f"  goto:    {it.goto}")
        print(f"  content: {it.content}")
        print(f"  source:  {it.source}  size={it.width}x{it.height}  "
              f"date={it.date}  image_url={it.image_url}")
        print()

    failures: list[str] = []
    if len(items) != len(EXPECTED):
        failures.append(f"条数不符：期望 {len(EXPECTED)}，实际 {len(items)}")
    for i, (url, direct, content, source, width, height, date) in enumerate(EXPECTED):
        if i >= len(items):
            break
        got = items[i]
        if got.url != url:
            failures.append(f"[{i}] url 期望 {url} 实际 {got.url}")
        if direct and got.goto is not None:
            failures.append(f"[{i}] 应是直链，却带了 goto={got.goto}")
        if not direct and not got.goto:
            failures.append(f"[{i}] 应带 goto 跳板，实际为空")
        if got.content != content:
            failures.append(f"[{i}] content\n    期望 {content}\n    实际 {got.content}")
        if got.source != source:
            failures.append(f"[{i}] source 期望 {source} 实际 {got.source}")
        if (got.width, got.height) != (width, height):
            failures.append(f"[{i}] 尺寸期望 {width}x{height} "
                            f"实际 {got.width}x{got.height}")
        if got.date != date:
            failures.append(f"[{i}] date 期望 {date} 实际 {got.date}")

    pixiv = [i for i in items if i.url and "pixiv.net" in i.url]
    if pixiv and pixiv[0].image_url != "https://img.example-cdn.net/bunny.jpg":
        failures.append(f"imgurl 未还原：{pixiv[0].image_url}")

    # parse_extracted 只保留能直接拿到地址的，goto 那条应被跳过
    direct_only = parse_extracted(payload, max_results=20)
    if len(direct_only) != 4:
        failures.append(f"parse_extracted 期望 4 条直链，实际 {len(direct_only)}")

    if failures:
        print("=== 校验失败 ===")
        for f in failures:
            print(" ✗", f)
        return 1
    print("=== 校验通过：四种链接形态 + 行结构解析均正常 ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
