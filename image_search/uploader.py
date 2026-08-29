"""Google Lens 上传相关的参数构造与 URL 改写。

实际的请求发送在 :mod:`image_search.session` 里 —— 上传和后续的
``qfmetadata`` 必须共用同一个会话，所以统一交给 ``LensSession`` 管。
"""

from __future__ import annotations

import time
import urllib.parse as up

from .config import UDM_EXACT_MATCHES

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


def to_exact_matches_url(location: str, hl: str) -> str:
    """把上传返回的结果页地址改写成 "Exact matches"（完全匹配）标签页。

    上传后 Google 默认给的是 ``udm=26``（全部结果），改成 ``udm=48``
    就是完全匹配那一栏。
    """
    parts = up.urlsplit(location)
    query = dict(up.parse_qsl(parts.query))
    query["udm"] = UDM_EXACT_MATCHES
    query["hl"] = hl
    return up.urlunsplit((parts.scheme, parts.netloc, parts.path,
                          up.urlencode(query), ""))
