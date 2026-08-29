"""Google Lens 上传相关的参数构造与 URL 改写。

实际的请求发送在 :mod:`image_search.session` 里 —— 上传和后续的
``qfmetadata`` 必须共用同一个会话，所以统一交给 ``LensSession`` 管。
"""

from __future__ import annotations

import time
import urllib.parse as up

from .config import UDM_AI_MODE, UDM_EXACT_MATCHES

UPLOAD_URL = "https://lens.google.com/v3/upload"


def build_upload_params(hl: str) -> dict[str, str]:
    """构造上传参数。

    ``ep`` / ``re`` 标识入口来源，``stcs`` 是客户端时间戳，
    ``vpw`` / ``vph`` 是视口尺寸 —— 都是 Lens 网页版自己会带的参数。
    """
    return {
        "hl": hl,
        "re": "df",
        "stcs": str(int(time.time() * 1000)),
        "vpw": "1920",
        "vph": "1080",
        "ep": "subb",
    }


def to_mode_url(location: str, hl: str, udm: str,
                safe_search: bool = False) -> str:
    """把上传返回的结果页地址改写成指定标签页。

    上传后 Google 默认给 ``udm=26``（全部结果）。同一个 ``vsrid`` 只要换 ``udm``
    就能切到别的标签，不需要重新上传 —— 这是「一次上传、多种结果」的基础。

    ``safe`` 显式带上而不是省略：Google 的默认值随出口 IP 所在地区变化，
    部分地区强制开启过滤。实测 ``safe=active`` 会把命中过滤的结果直接清空，
    而症状和「图片没被收录」完全一样，无从分辨。
    """
    parts = up.urlsplit(location)
    query = dict(up.parse_qsl(parts.query))
    query["udm"] = udm
    query["hl"] = hl
    query["safe"] = "active" if safe_search else "off"
    return up.urlunsplit((parts.scheme, parts.netloc, parts.path,
                          up.urlencode(query), ""))


def to_exact_matches_url(location: str, hl: str,
                         safe_search: bool = False) -> str:
    """改写成 "Exact matches"（完全匹配）标签页，即 ``udm=48``。"""
    return to_mode_url(location, hl, UDM_EXACT_MATCHES, safe_search)


def to_ai_mode_url(location: str, hl: str, safe_search: bool = False) -> str:
    """改写成 "AI Mode"（AI 模式）标签页，即 ``udm=50``。"""
    return to_mode_url(location, hl, UDM_AI_MODE, safe_search)
