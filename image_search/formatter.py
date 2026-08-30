"""把搜索结果格式化成可直接发送的文本。

输出分两层：

* :func:`format_blocks` 把结果拆成若干**独立的消息块**，由调用方决定是逐条
  发送、还是打包成一条合并转发。拆分粒度由 :class:`OutputOptions` 控制。
* :func:`format_result` 把这些块拼成一段文本，给本地脚本和 LLM 工具用。

插件和本地测试脚本共用同一套格式化逻辑，保证两边看到的内容一致。
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
        header: 完全匹配列表前面的抬头。``{count}`` 会替换成条数，不写也可以。
        ai_header: AI 描述前面的抬头；留空则直接输出描述。
        empty_text: 没有完全匹配结果时的文案。
        expect_exact_matches: 是否开启了完全匹配搜索。开着却一条都没有时，
            要明确告知 ``empty_text``，而不是只把 AI 描述发出去 —— 否则用户
            分不清「没有收录这张图」和「插件没搜完全匹配」。
        use_forward_message: 是否把结果打包成一条合并转发消息。
        link_as_separate_message: 是否把每条结果的链接单独拆成一个消息块，
            方便长按复制。只在合并转发下有意义 —— 普通消息逐条发会刷屏，
            所以 :func:`format_blocks` 里要求 ``use_forward_message`` 同时为真。
        merge_ai_and_exact: 是否把 AI 描述和完全匹配合并进同一个块。
            和 ``use_forward_message`` 无关，两者可以任意组合。
        separator: 拆分链接时插在结果之间的分隔块内容。
    """

    limit: int = 10
    show_source: bool = True
    show_size: bool = False
    show_index: bool = True
    show_ocr: bool = False
    header: str = "找到以下结果"
    ai_header: str = "【图片描述】"
    empty_text: str = "没有找到完全匹配的结果"
    expect_exact_matches: bool = True
    use_forward_message: bool = True
    link_as_separate_message: bool = False
    merge_ai_and_exact: bool = False
    separator: str = "————————"


def format_match_info(match: ExactMatch, options: OutputOptions,
                      index: int | None = None) -> str:
    """格式化一条结果里除链接以外的部分（标题 / 来源 / 尺寸）。"""
    prefix = f"{index}. " if (options.show_index and index is not None) else ""
    lines = [f"{prefix}标题: {match.content}"]
    if options.show_source and match.source:
        lines.append(f"来源: {match.source}")
    if options.show_size and match.width and match.height:
        lines.append(f"尺寸: {match.width}x{match.height}")
    return "\n".join(lines)


def format_match_link(match: ExactMatch) -> str:
    """格式化一条结果的链接部分。"""
    return f"链接: {match.url}"


def format_match(match: ExactMatch, options: OutputOptions,
                 index: int | None = None) -> str:
    """格式化完整的一条结果，链接放最后一行方便复制。"""
    return (f"{format_match_info(match, options, index)}\n"
            f"{format_match_link(match)}")


def _exact_blocks(result: LensSearchResult,
                  options: OutputOptions) -> list[str]:
    """把完全匹配部分拆成消息块。"""
    shown = result.exact_matches[: max(1, options.limit)]
    if not shown:
        return []

    head = (options.header.format(count=len(result.exact_matches))
            if options.header else "")

    # 链接单独成块只在合并转发下开放：普通消息里逐条发会刷屏
    if options.link_as_separate_message and options.use_forward_message:
        blocks = [head] if head else []
        for index, match in enumerate(shown, 1):
            if index > 1 and options.separator:
                blocks.append(options.separator)
            blocks.append(format_match_info(match, options, index))
            blocks.append(format_match_link(match))
        return blocks

    body = "\n\n".join(format_match(m, options, i)
                       for i, m in enumerate(shown, 1))
    return [f"{head}\n{body}" if head else body]


def format_blocks(result: LensSearchResult,
                  options: OutputOptions | None = None) -> list[str]:
    """把搜索结果拆成若干独立的消息块。

    两种结果模式各自独立：只要一种有内容就正常输出，两种都空才回落到
    ``empty_text``。所以关掉完全匹配、只留 AI 描述也能正常工作。

    Returns:
        非空的文本块列表。列表长度就是调用方要发的消息条数（或合并转发的
        节点数），至少有一个元素。
    """
    options = options or OutputOptions()

    ai_block = ""
    if result.ai_summary:
        ai_block = (f"{options.ai_header}\n{result.ai_summary}"
                    if options.ai_header else result.ai_summary)

    blocks = _exact_blocks(result, options)
    if not blocks and options.expect_exact_matches:
        # 开着完全匹配却一条没有，必须说一句。只发 AI 描述会让人以为
        # 插件压根没去搜，或者以为这张图确实没被收录
        blocks = [options.empty_text]

    if ai_block:
        if blocks and options.merge_ai_and_exact:
            # 合并时并入第一块。拆分链接的情况下第一块是抬头，AI 描述就落在
            # 列表前面，视觉上仍是「描述 + 结果」的顺序
            blocks[0] = f"{ai_block}\n\n{blocks[0]}"
        else:
            blocks.insert(0, ai_block)

    if not blocks:
        blocks = [options.empty_text]
    if options.show_ocr and result.ocr_text:
        blocks.append(f"图中文字：\n{result.ocr_text}")
    return blocks


def format_result(result: LensSearchResult,
                  options: OutputOptions | None = None) -> str:
    """把整个搜索结果格式化成一段文本。

    给本地脚本和 LLM 工具用 —— 它们只要一段纯文本，所以这里强制不拆分链接，
    免得分隔块变成正文里莫名其妙的横线。
    """
    options = options or OutputOptions()
    if options.link_as_separate_message:
        options = dataclasses.replace(options, link_as_separate_message=False)
    return "\n\n".join(format_blocks(result, options))
