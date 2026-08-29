"""图片输入统一加载：本地路径 / URL / 原始字节 都归一成 (bytes, filename, mime)。

动图（GIF / 动态 WebP / APNG）会先抽出第一帧转成 PNG 再上传，原因见
:func:`extract_first_frame`。
"""

from __future__ import annotations

import io
import pathlib
import urllib.parse as up
from typing import Union

from .config import SearchConfig
from .exceptions import ImageSearchError
from .logger import logger, quiet_image_logs

ImageInput = Union[bytes, bytearray, str, pathlib.Path]

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

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

# 只有这几种格式可能是多帧。JPEG / BMP 不可能，跳过可以省一次解码
_MAYBE_ANIMATED = {"image/gif", "image/webp", "image/png", "image/apng"}


def extract_first_frame(data: bytes, mime: str) -> bytes | None:
    """动图抽第一帧转成 PNG。不是动图、或抽帧失败时返回 ``None``（按原样上传）。

    先说清楚它**不**解决什么：动图原样传给 Lens 它也认，而且实测结果和抽帧
    完全一样（69 帧的 GIF，两种方式都返回同一批 5 条结果，连顺序都相同）——
    至少对测试用的这张图，Google 自己取的就是第一帧。所以抽帧不是为了提高
    命中率。

    真正的收益有两条：

    * **体积**。实测 1851 KB 的 GIF 抽帧后只有 205 KB。GIF 动辄几 MB 到几十
      MB，抽帧后才有可能落在上传上限内，走代理时也省带宽。
    * **确定性**。Google 用哪一帧是它的实现细节，没有承诺，也可能随格式和
      时间变化。抽帧后"搜的是哪张图"由我们决定，出问题时可复现。

    透明通道会保留下来 —— GIF 的调色板透明色如果直接转 RGB，透明区域会被
    填成黑色，画面内容就变了，反搜自然搜不中。
    """
    if mime not in _MAYBE_ANIMATED:
        return None
    try:
        from PIL import Image
    except ImportError:
        logger.debug("没装 Pillow，动图按原样上传")
        return None
    quiet_image_logs()

    try:
        with Image.open(io.BytesIO(data)) as image:
            frames = getattr(image, "n_frames", 1)
            if frames <= 1:
                return None
            image.seek(0)
            has_alpha = (image.mode in ("RGBA", "LA")
                         or "transparency" in image.info)
            frame = image.convert("RGBA" if has_alpha else "RGB")
            buffer = io.BytesIO()
            frame.save(buffer, format="PNG", optimize=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("抽第一帧失败，按原样上传: %s: %s", type(exc).__name__, exc)
        return None

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


async def load_image(source: ImageInput, config: SearchConfig) -> tuple[bytes, str, str]:
    """把各种输入形式统一成 ``(数据, 文件名, mime)``。"""
    data: bytes
    name = "image"

    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        text = str(source)
        if text.startswith(("http://", "https://")):
            import httpx

            async with httpx.AsyncClient(timeout=config.timeout_ms / 1000,
                                         proxy=config.proxy,
                                         follow_redirects=True) as client:
                try:
                    resp = await client.get(
                        text, headers={"User-Agent": config.user_agent})
                    resp.raise_for_status()
                except Exception as exc:  # noqa: BLE001
                    raise ImageSearchError(
                        f"下载图片失败: {type(exc).__name__}: {exc}") from exc
                data = resp.content
            name = pathlib.Path(up.urlsplit(text).path).name or "image"
        else:
            path = pathlib.Path(text)
            if not path.is_file():
                raise ImageSearchError(f"图片文件不存在: {path}")
            data = path.read_bytes()
            name = path.name

    if not data:
        raise ImageSearchError("图片内容为空")
    # 这道闸只防内存被解码撑爆，真正的上传上限在抽帧之后才判
    if len(data) > MAX_SOURCE_BYTES:
        raise ImageSearchError(
            f"图片过大（{len(data) / 1048576:.1f} MB），"
            f"上限 {MAX_SOURCE_BYTES / 1048576:.0f} MB")

    mime, ext = sniff_mime(data)
    if mime == "application/octet-stream":
        suffix = pathlib.Path(name).suffix.lower()
        mime = _MIME_BY_SUFFIX.get(suffix, "image/png")
        ext = (suffix.lstrip(".") or "png")
    if "." not in name:
        name = f"{name}.{ext}"

    # 抽帧要在大小判定之前：几十 MB 的 GIF 抽出来的单帧往往只有几百 KB，
    # 先判大小会把这类图白白拒掉
    frame = extract_first_frame(data, mime)
    if frame is not None:
        data, mime = frame, "image/png"
        name = f"{pathlib.Path(name).stem}_frame0.png"

    if len(data) > MAX_IMAGE_BYTES:
        raise ImageSearchError(
            f"图片过大（{len(data) / 1048576:.1f} MB），"
            f"上限 {MAX_IMAGE_BYTES / 1048576:.0f} MB")
    return data, name, mime
