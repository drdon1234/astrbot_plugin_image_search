"""图片输入统一加载：本地路径 / URL / 原始字节都归一为上传数据。"""

from __future__ import annotations

import asyncio
import io
import os
import pathlib
import re
import stat
import urllib.parse as up
import warnings
from typing import Union

from .config import SearchConfig
from .exceptions import ImageInputError
from .http_client import ResponseTooLargeError, fetch_limited
from .logger import (
    exception_for_log,
    logger,
    path_for_log,
    quiet_http_logs,
    quiet_image_logs,
)

ImageInput = Union[bytes, bytearray, str, pathlib.Path]
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")

# 通过文件头判断类型，比扩展名可靠
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
    (b"BM", "image/bmp", "bmp"),
)

# 最终上传的数据上限
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# 读入时的硬上限。动图抽帧后通常会小一个数量级，所以入口这道闸放宽些，
# 让大 GIF 有机会被抽帧救回来；但仍要有上限，否则 Pillow 解码会吃满内存
MAX_SOURCE_BYTES = 64 * 1024 * 1024
# 解码后的像素上限。约等于一张 7680x4320 图片，能覆盖常见 8K 素材，
# 同时阻止小体积压缩炸弹扩张到不可控内存。
MAX_IMAGE_PIXELS = 40_000_000

# 只有这几种格式可能是多帧。JPEG / BMP 不可能，跳过可以省一次解码
_MAYBE_ANIMATED = {"image/gif", "image/webp", "image/png", "image/apng"}


def extract_first_frame(data: bytes, mime: str) -> bytes | None:
    """验证图片并在动图时抽第一帧转成 PNG。

    静态图会完整解码验证后返回 ``None``。调用方应在线程中运行本函数，避免
    Pillow 的 CPU 和文件解析工作阻塞事件循环。
    """
    if mime not in _MAYBE_ANIMATED:
        may_be_animated = False
    else:
        may_be_animated = True
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImageInputError("缺少 Pillow，无法安全验证图片格式") from exc
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                width, height = probe.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ImageInputError(
                        f"图片像素过大（{width}x{height}），"
                        f"上限 {MAX_IMAGE_PIXELS / 1_000_000:.0f} 百万像素"
                    )
                probe.verify()

            with Image.open(io.BytesIO(data)) as image:
                frames = getattr(image, "n_frames", 1) if may_be_animated else 1
                image.seek(0)
                if frames <= 1:
                    image.load()
                    return None
                has_alpha = (
                    image.mode in ("RGBA", "LA") or "transparency" in image.info
                )
                frame = image.convert("RGBA" if has_alpha else "RGB")
                buffer = io.BytesIO()
                frame.save(buffer, format="PNG", optimize=True)
    except ImageInputError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageInputError(
            f"图片像素超过 {MAX_IMAGE_PIXELS / 1_000_000:.0f} 百万像素上限"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise ImageInputError("图片格式无效、内容损坏或无法完整解码") from exc

    payload = buffer.getvalue()
    logger.debug("动图共 %d 帧，取第一帧转 PNG（%d -> %d 字节）",
                 frames, len(data), len(payload))
    return payload


def sniff_mime(data: bytes) -> tuple[str, str]:
    """返回 (mime, 扩展名)。"""
    for magic, mime, ext in _MAGIC:
        if data.startswith(magic):
            return mime, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return "application/octet-stream", "bin"


def _read_local_file(path: pathlib.Path) -> bytes:
    """在读入前后核对普通文件与大小，缩小检查和读取之间的竞态窗口。"""
    try:
        before = path.stat()
    except (OSError, ValueError) as exc:
        logger.debug("图片文件无法访问 %s: %s",
                     path_for_log(path), exception_for_log(exc))
        raise ImageInputError("图片文件无法访问") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ImageInputError("图片文件不存在或不是普通文件")
    if before.st_size > MAX_SOURCE_BYTES:
        raise ImageInputError(
            f"图片过大（{before.st_size / 1048576:.1f} MB），"
            f"上限 {MAX_SOURCE_BYTES / 1048576:.0f} MB"
        )

    try:
        with path.open("rb") as file:
            opened = os.fstat(file.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ImageInputError("图片文件不存在或不是普通文件")
            if opened.st_size > MAX_SOURCE_BYTES:
                raise ImageInputError(
                    f"图片过大（{opened.st_size / 1048576:.1f} MB），"
                    f"上限 {MAX_SOURCE_BYTES / 1048576:.0f} MB"
                )
            data = file.read(MAX_SOURCE_BYTES + 1)
    except ImageInputError:
        raise
    except OSError as exc:
        logger.debug("读取图片文件失败 %s: %s",
                     path_for_log(path), exception_for_log(exc))
        raise ImageInputError("读取图片文件失败") from exc
    return data


def _looks_like_url(value: str) -> bool:
    return bool(_URL_SCHEME_RE.match(value))


async def _download_image(url: str, config: SearchConfig) -> tuple[bytes, str]:
    """通过普通 HTTP 客户端下载图片，同时限制内存占用。"""
    import httpx

    timeout = config.timeout_ms / 1000

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            proxy=config.proxy,
        ) as client:
            response = await fetch_limited(
                client,
                url,
                max_bytes=MAX_SOURCE_BYTES,
                headers={"User-Agent": config.user_agent},
                total_timeout_seconds=timeout,
            )
        return response.content, response.url
    except ResponseTooLargeError as exc:
        raise ImageInputError(
            f"图片过大，上限 {MAX_SOURCE_BYTES / 1048576:.0f} MB"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise ImageInputError(
            f"下载图片失败: HTTP {exc.response.status_code}"
        ) from exc
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        raise ImageInputError("下载图片超时") from exc
    except Exception as exc:  # noqa: BLE001
        raise ImageInputError(f"下载图片失败: {type(exc).__name__}") from exc


async def load_image(source: ImageInput, config: SearchConfig) -> tuple[bytes, str, str]:
    """把各种输入形式统一成 ``(数据, 文件名, mime)``。"""
    data: bytes
    name = "image"

    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        text = str(source)
        if _looks_like_url(text):
            with quiet_http_logs():
                data, response_url = await _download_image(text, config)
            name = pathlib.PurePosixPath(
                up.urlsplit(response_url).path
            ).name or "image"
        else:
            path = pathlib.Path(text)
            data = await asyncio.to_thread(_read_local_file, path)
            name = path.name

    if not data:
        raise ImageInputError("图片内容为空")
    # 这道闸只防内存被解码撑爆，真正的上传上限在抽帧之后才判
    if len(data) > MAX_SOURCE_BYTES:
        raise ImageInputError(
            f"图片过大（{len(data) / 1048576:.1f} MB），"
            f"上限 {MAX_SOURCE_BYTES / 1048576:.0f} MB")

    mime, ext = sniff_mime(data)
    if mime == "application/octet-stream":
        raise ImageInputError(
            "不支持或无法识别图片格式；仅接受 PNG、JPEG、GIF、WebP、BMP"
        )
    if "." not in name:
        name = f"{name}.{ext}"

    # 抽帧要在大小判定之前：几十 MB 的 GIF 抽出来的单帧往往只有几百 KB，
    # 先判大小会把这类图白白拒掉
    with quiet_image_logs():
        frame = await asyncio.to_thread(extract_first_frame, data, mime)
    if frame is not None:
        data, mime = frame, "image/png"
        name = f"{pathlib.Path(name).stem}_frame0.png"

    if len(data) > MAX_IMAGE_BYTES:
        raise ImageInputError(
            f"图片过大（{len(data) / 1048576:.1f} MB），"
            f"上限 {MAX_IMAGE_BYTES / 1048576:.0f} MB")
    return data, name, mime
