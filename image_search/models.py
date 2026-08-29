"""搜索结果数据模型。"""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(slots=True)
class ExactMatch:
    """Google Lens "Exact matches"（完全匹配）中的一条结果。

    Attributes:
        url: 收录该图片的来源页面地址。
        content: 结果标题（Lens 给出的页面标题 / 图片描述）。
        source: 站点名称，例如 ``shop.lashinbang.com``。
        thumbnail: 缩略图地址，可能是 https 链接或 data URI。
        image_url: 原始大图地址（部分结果没有）。
        width: 原图宽度（像素），未知为 None。
        height: 原图高度（像素），未知为 None。
        date: Google 给出的收录日期，没有则为 None。
    """

    url: str
    content: str
    source: str | None = None
    thumbnail: str | None = None
    image_url: str | None = None
    width: int | None = None
    height: int | None = None
    date: str | None = None

    @property
    def truncated(self) -> bool:
        """标题是否被 Google 截断过。"""
        return self.content.rstrip().endswith(("...", "…"))

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        """按 ``链接: ... / 标题: ...`` 形式格式化。"""
        return f"链接: {self.url}\n标题: {self.content}"


@dataclasses.dataclass(slots=True)
class LensSearchResult:
    """一次 Lens 反搜的完整结果。"""

    exact_matches: list[ExactMatch] = dataclasses.field(default_factory=list)
    result_url: str = ""
    lens_url: str = ""
    ocr_text: str = ""
    engine: str = "google_lens"

    def __bool__(self) -> bool:
        return bool(self.exact_matches)

    def __len__(self) -> int:
        return len(self.exact_matches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "result_url": self.result_url,
            "lens_url": self.lens_url,
            "ocr_text": self.ocr_text,
            "exact_matches": [m.to_dict() for m in self.exact_matches],
        }

    def format(self, limit: int = 10) -> str:
        if not self.exact_matches:
            return "未找到完全匹配的结果"
        blocks = [m.format() for m in self.exact_matches[:limit]]
        return "\n\n".join(blocks)
