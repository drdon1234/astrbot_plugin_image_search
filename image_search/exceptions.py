"""图片搜索模块异常定义。"""

from __future__ import annotations


class ImageSearchError(Exception):
    """本模块所有异常的基类。"""


class ImageInputError(ImageSearchError):
    """图片输入不合法；异常文字由 loader 生成，可经脱敏后展示给用户。"""


class UploadError(ImageSearchError):
    """图片上传到 Google Lens 失败。"""


class FetchError(ImageSearchError):
    """抓取结果页失败。"""


class RateLimitedError(FetchError):
    """被 Google 限流 / 要求人机验证（/sorry/index）。"""


class ParseError(ImageSearchError):
    """结果页结构无法解析，通常意味着 Google 改版了。"""


class BrowserNotAvailableError(ImageSearchError):
    """需要 Playwright 但环境不可用。"""


class SearchBusyError(ImageSearchError):
    """搜索执行槽和有限等待队列都已占满。"""


class SearchTimeoutError(TimeoutError):
    """请求取得执行权后超过配置的总时限。"""
