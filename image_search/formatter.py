"""把搜索结果格式化成可直接发送的文本。

插件和本地测试脚本共用同一套格式化逻辑，保证两边看到的输出一致。
"""

from __future__ import annotations

import dataclasses

from .models import ExactMatch, LensSearchResult


@dataclasses.dataclass(slots=True)
class OutputOptions:
    """输出格式选项。

    Attributes:
        limit: 最多输出几条。
        show_source: 是否输出站点名。
        show_size: 是否输出图片尺寸。
        show_index: 是否给每条加序号。
        show_ocr: 是否附上 OCR 文字。
        header: 结果前面的抬头。``{count}`` 会替换成条数，不写也可以。
        empty_text: 没有结果时的文案。
    """

    limit: int = 10
    show_source: bool = True
    show_size: bool = False
    show_index: bool = True
    show_ocr: bool = False
    header: str = "找到以下结果"
    empty_text: str = "没有找到完全匹配的结果"


def format_match(match: ExactMatch, options: OutputOptions,
                 index: int | None = None) -> str:
    """按 ``链接`` / ``标题`` 两行的形式格式化一条结果。"""
    prefix = f"{index}. " if (options.show_index and index is not None) else ""
    lines = [f"{prefix}链接: {match.url}", f"标题: {match.content}"]
    extras: list[str] = []
    if options.show_source and match.source:
        extras.append(f"来源: {match.source}")
    if options.show_size and match.width and match.height:
        extras.append(f"尺寸: {match.width}x{match.height}")
    lines.extend(extras)
    return "\n".join(lines)


def format_result(result: LensSearchResult,
                  options: OutputOptions | None = None) -> str:
    """把整个搜索结果格式化成一段文本。"""
    options = options or OutputOptions()
    if not result.exact_matches:
        text = options.empty_text
        if options.show_ocr and result.ocr_text:
            text += f"\n\n图中文字：\n{result.ocr_text}"
        return text

    shown = result.exact_matches[: max(1, options.limit)]
    blocks = [format_match(m, options, i) for i, m in enumerate(shown, 1)]
    parts: list[str] = []
    if options.header:
        parts.append(options.header.format(count=len(result.exact_matches)))
    parts.append("\n\n".join(blocks))
    if options.show_ocr and result.ocr_text:
        parts.append(f"图中文字：\n{result.ocr_text}")
    return "\n\n".join(parts)
