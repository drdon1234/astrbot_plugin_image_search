"""Google Lens 图片反搜模块。

不依赖 AstrBot，可以单独当库用：

    import asyncio
    from image_search import GoogleLensSearcher, SearchConfig

    async def main():
        async with GoogleLensSearcher(SearchConfig()) as searcher:
            result = await searcher.search("test_imgs/test.png")
            for match in result.exact_matches:
                print(match.url, match.content)

    asyncio.run(main())

常驻进程（比如机器人插件）用 :class:`LensSearchService`，它会懒启动浏览器并在
空闲后自动关闭。
"""

from .config import SearchConfig
from .exceptions import (
    BrowserNotAvailableError,
    FetchError,
    ImageSearchError,
    ParseError,
    RateLimitedError,
    UploadError,
)
from .formatter import OutputOptions, format_match, format_result
from .metadata import parse_ocr_lines
from .models import ExactMatch, LensSearchResult
from .plugin_config import PluginConfig, PluginOptions, build_config
from .searcher import GoogleLensSearcher, search_image
from .service import LensSearchService
from .session import LensSession

__all__ = [
    "BrowserNotAvailableError",
    "ExactMatch",
    "FetchError",
    "GoogleLensSearcher",
    "ImageSearchError",
    "LensSearchResult",
    "LensSearchService",
    "LensSession",
    "OutputOptions",
    "ParseError",
    "PluginConfig",
    "PluginOptions",
    "RateLimitedError",
    "SearchConfig",
    "UploadError",
    "build_config",
    "format_match",
    "format_result",
    "parse_ocr_lines",
    "search_image",
]

__version__ = "0.1.0"
