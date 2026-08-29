"""日志。

在 AstrBot 里跑时用 AstrBot 的 logger（日志会进 WebUI），独立跑本地脚本时
退回标准库 logging。这样 ``image_search`` 包本身不依赖 AstrBot，既能被插件
导入，也能单独测试。
"""

from __future__ import annotations

import logging

try:  # pragma: no cover - 取决于运行环境
    from astrbot.api import logger as _logger
except Exception:  # noqa: BLE001
    _logger = logging.getLogger("astrbot_plugin_image_search")

logger = _logger

_quieted: set[str] = set()


def _quiet(*names: str) -> None:
    """把第三方库的 logger 提到 WARNING。幂等，每个名字只设一次。

    注意这是**进程级**设置，同进程里其他用到这些库的代码也会受影响。
    权衡后仍这么做：下面这几个 logger 的 DEBUG 输出基本只有排查库本身
    才用得上，留着会把真正有用的日志淹掉。
    """
    for name in names:
        if name in _quieted:
            continue
        logging.getLogger(name).setLevel(logging.WARNING)
        _quieted.add(name)


def quiet_http_logs() -> None:
    """压掉 httpx / httpcore 的低级别日志。

    httpcore 会给每个请求发十几条 DEBUG trace（``connect_tcp.started``、
    ``send_request_headers.complete`` 之类），httpx 自己也会每个请求一行
    ``HTTP Request: GET ... 302``。一次搜索要还原二十来个跳板链接，在开了
    DEBUG 的环境里这些 trace 能刷几百行 —— 而且里面还包含完整的
    Set-Cookie 响应头。关键信息本模块自己会用 ``logger.debug`` 记。
    """
    _quiet("httpx", "httpcore")


def quiet_image_logs() -> None:
    """压掉 Pillow 的 DEBUG 日志。

    PIL 解码时会为每个数据块打一行（``STREAM b'IHDR'``、``STREAM b'IDAT'``），
    一张图就是好几行。抽帧是内部实现细节，不该出现在日志里。
    """
    _quiet("PIL")


__all__ = ["logger", "quiet_http_logs", "quiet_image_logs"]
