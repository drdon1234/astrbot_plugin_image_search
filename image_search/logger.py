"""日志边界与第三方噪声的任务级作用域控制。"""

from __future__ import annotations

import contextlib
import contextvars
import ipaddress
import logging
import re
import threading
import urllib.parse
from collections.abc import Iterator

try:  # pragma: no cover - 取决于运行环境
    from astrbot.api import logger as _logger
except Exception:  # noqa: BLE001
    _logger = logging.getLogger("astrbot_plugin_image_search")

logger = _logger

_quiet_http_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "image_search_quiet_http_depth", default=0)
_quiet_image_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "image_search_quiet_image_depth", default=0)
_filter_lock = threading.Lock()

_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+")
_SCHEME_RELATIVE_URL_RE = re.compile(r"(?<![:/])//[^\s<>\"']+")
_KEY_NAME = r"[a-z][a-z0-9_-]*"
_KEY_ASSIGNMENT_RE = re.compile(
    rf"(?im)(?P<prefix>[\"']?\b(?P<key>{_KEY_NAME})\b[\"']?\s*[:=]\s*)"
    r"[^\r\n]*")
_KEY_PAIR_RE = re.compile(
    rf"(?im)(?P<prefix>[\"'](?P<key>{_KEY_NAME})[\"']\s*,\s*)[^\r\n]+")
_KEY_OPTION_RE = re.compile(
    rf"(?im)(?P<prefix>-{{1,2}}(?P<key>{_KEY_NAME})\s+)[^\r\n]+")
_INDEX_ASSIGNMENT_RE = re.compile(
    rf"(?im)(?P<prefix>\[\s*(?P<quote>[\"']?)(?P<key>{_KEY_NAME})"
    rf"(?P=quote)\s*\]\s*=\s*)"
    r"[^\r\n]*")
_PERCENT_ASSIGNMENT_RE = re.compile(
    rf"(?im)(?P<prefix>(?P<key>{_KEY_NAME})%(?:3a|3d))[^&\s\r\n]+")
_XML_ELEMENT_RE = re.compile(
    rf"(?is)<(?P<key>{_KEY_NAME})\b[^>]*>.*?</(?P=key)\s*>")
_SECRET_HEADER_RE = re.compile(
    r"(?im)\b(authorization|proxy-authorization|cookie|set-cookie)\s*:[^\r\n]*")
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(?:basic|bearer)\s+[^\s,;]+")
_COOKIE_OBJECT_RE = re.compile(
    r"(?is)(?:<cookies?\b|\bcookiejar\b).*")
_PRIVATE_KEY_RE = re.compile(
    r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY[^-\r\n]*-----.*")
_JWT_RE = re.compile(
    r"(?<![a-zA-Z0-9_-])eyJ[a-zA-Z0-9_-]+\."
    r"[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+(?![a-zA-Z0-9_-])")
_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![\w])(?:[a-z]:[\\/])[^,;\r\n\"'<>|]+")
_UNC_PATH_RE = re.compile(
    r"(?<![\\\w])\\\\[^\\/\s,;\r\n\"'<>|]+\\[^,;\r\n\"'<>|]*")
_POSIX_PATH_RE = re.compile(
    r"(?<![\w:/])/(?!/)[^,;\r\n\"'<>|]+")
_IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6_RE = re.compile(
    r"(?<![\w:])(?:\[[0-9a-fA-F:.%]+\]|"
    r"(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?:%[\w.-]+)?)(?![\w:])")


class _ScopedThirdPartyFilter(logging.Filter):
    """只在本插件的当前执行上下文中压掉第三方低级别日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        name = record.name
        if _quiet_http_depth.get() and (
            name == "httpx"
            or name.startswith("httpx.")
            or name == "httpcore"
            or name.startswith("httpcore.")
        ):
            return False
        if _quiet_image_depth.get() and (
            name == "PIL" or name.startswith("PIL.")
        ):
            return False
        return True


_scoped_filter = _ScopedThirdPartyFilter()


def _install_scoped_filter() -> None:
    """给当前已有 handler 装一次无副作用 filter。

    Logger 自身的 filter 不会处理子 logger 向上冒泡的记录，所以这里挂在
    handler 上。filter 在作用域外始终返回 ``True``，不会改变同进程其它模块
    的日志行为；作用域状态由 ``ContextVar`` 隔离到 task，并会被
    :func:`asyncio.to_thread` 自动复制到工作线程。
    """
    handlers: list[logging.Handler] = list(logging.getLogger().handlers)
    last_resort = logging.lastResort
    if last_resort is not None:
        handlers.append(last_resort)
    # copy() 在这里取一个稳定快照，避免其它线程同时创建 logger 时改变字典。
    for item in logging.root.manager.loggerDict.copy().values():
        if isinstance(item, logging.Logger):
            handlers.extend(item.handlers)

    with _filter_lock:
        seen: set[int] = set()
        for handler in handlers:
            if id(handler) in seen:
                continue
            seen.add(id(handler))
            try:
                if not any(item is _scoped_filter for item in handler.filters):
                    handler.addFilter(_scoped_filter)
            except Exception:  # noqa: BLE001 - 日志配置不能打断搜索
                continue


@contextlib.contextmanager
def quiet_http_logs() -> Iterator[None]:
    """在当前 task/thread 内压掉 httpx/httpcore 的 DEBUG/INFO 日志。"""
    _install_scoped_filter()
    token = _quiet_http_depth.set(_quiet_http_depth.get() + 1)
    try:
        yield
    finally:
        _quiet_http_depth.reset(token)


@contextlib.contextmanager
def quiet_image_logs() -> Iterator[None]:
    """在当前 task/thread 内压掉 Pillow 的 DEBUG/INFO 日志。"""
    _install_scoped_filter()
    token = _quiet_image_depth.set(_quiet_image_depth.get() + 1)
    try:
        yield
    finally:
        _quiet_image_depth.reset(token)


def url_for_log(value: object) -> str:
    """仅保留 URL 的协议与主机，移除凭据、路径、查询参数和片段。"""
    try:
        parsed = urllib.parse.urlsplit(str(value))
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        port = parsed.port
    except (TypeError, ValueError):
        return "<redacted-url>"
    if scheme not in {"http", "https"} or not host:
        return "<redacted-url>"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 80 if scheme == "http" else 443
    authority = host if port in (None, default_port) else f"{host}:{port}"
    return f"<{scheme}://{authority}>"


def path_for_log(_value: object) -> str:
    """隐藏本地绝对路径及文件名。"""
    return "<local-path>"


def _is_secret_key(value: str) -> bool:
    """按字段语义识别 snake/kebab/camel/Pascal case 的凭据键。"""
    compact = re.sub(r"[^a-z0-9]", "", value.lower())
    markers = (
        "token",
        "secret",
        "password",
        "passwd",
        "passphrase",
        "credential",
        "cookie",
        "authorization",
        "apikey",
        "accesskey",
        "accountkey",
        "subscriptionkey",
        "consumerkey",
        "privatekey",
        "sharedaccesssignature",
        "sessionid",
    )
    return (
        compact in {"auth", "jwt", "key", "sid", "gsessionid", "lsessionid", "vsrid"}
        or any(marker in compact for marker in markers)
    )


def _redact_key_match(match: re.Match[str]) -> str:
    if not _is_secret_key(match.group("key")):
        return match.group(0)
    return f"{match.group('prefix')}<redacted>"


def _redact_xml_match(match: re.Match[str]) -> str:
    if not _is_secret_key(match.group("key")):
        return match.group(0)
    return "<redacted-secret-field>"


def _redact_multiline_secret_tail(text: str) -> str:
    """敏感字段进入多行值后保守遮蔽本次日志的剩余内容。"""
    matches = [
        match
        for pattern in (_KEY_ASSIGNMENT_RE, _INDEX_ASSIGNMENT_RE)
        for match in pattern.finditer(text)
    ]
    for match in sorted(matches, key=lambda item: item.start()):
        tail = text[match.start():]
        if _is_secret_key(match.group("key")) and ("\n" in tail or "\r" in tail):
            return f"{text[:match.start()]}{match.group('prefix')}<redacted>"
    return text


def sanitize_log_text(value: object) -> str:
    """清理异常或子进程输出里的 URL、凭据 token 与常见本地绝对路径。"""
    text = str(value)
    text = _PRIVATE_KEY_RE.sub("<redacted-private-key>", text)
    text = _COOKIE_OBJECT_RE.sub("<redacted-cookie>", text)
    text = _JWT_RE.sub("<redacted-jwt>", text)
    text = _SECRET_HEADER_RE.sub(
        lambda match: f"{match.group(1)}: <redacted>", text)
    text = _URL_RE.sub(lambda match: url_for_log(match.group(0)), text)
    text = _SCHEME_RELATIVE_URL_RE.sub("<redacted-url>", text)
    text = _redact_multiline_secret_tail(text)
    text = _KEY_ASSIGNMENT_RE.sub(_redact_key_match, text)
    text = _KEY_PAIR_RE.sub(_redact_key_match, text)
    text = _KEY_OPTION_RE.sub(_redact_key_match, text)
    text = _INDEX_ASSIGNMENT_RE.sub(_redact_key_match, text)
    text = _PERCENT_ASSIGNMENT_RE.sub(_redact_key_match, text)
    text = _XML_ELEMENT_RE.sub(_redact_xml_match, text)
    text = _AUTH_SCHEME_RE.sub("<redacted-auth>", text)
    text = _WINDOWS_PATH_RE.sub("<local-path>", text)
    text = _UNC_PATH_RE.sub("<local-path>", text)
    text = _POSIX_PATH_RE.sub("<local-path>", text)

    def redact_ip(match: re.Match[str]) -> str:
        candidate = match.group(0).strip("[]")
        candidate = candidate.split("%", 1)[0]
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return match.group(0)
        return "<redacted-ip>"

    text = _IPV4_RE.sub(redact_ip, text)
    return _IPV6_RE.sub(redact_ip, text)


def exception_for_log(exc: BaseException) -> str:
    """保留异常类型和脱敏后的诊断文字。"""
    detail = sanitize_log_text(exc)
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


__all__ = [
    "exception_for_log",
    "logger",
    "path_for_log",
    "quiet_http_logs",
    "quiet_image_logs",
    "sanitize_log_text",
    "url_for_log",
]
