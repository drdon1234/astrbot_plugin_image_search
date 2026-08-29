r"""通过 Windows 命名管道访问 mihomo 的外部控制 API。

Clash Verge 默认把 external-controller 关掉，只留 ``\\.\pipe\verge-mihomo``
命名管道。这里手写一个极简 HTTP over named pipe 客户端，用来：

* 列出可用节点和代理组
* 切换某个组的选中节点
* 测节点延迟

    python tools/clash_api.py list
    python tools/clash_api.py groups
    python tools/clash_api.py switch <组名> <节点名>
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

PIPE = r"\\.\pipe\verge-mihomo"


def request(method: str, path: str, body: Any = None,
            timeout: float = 10.0) -> tuple[int, Any]:
    """向命名管道发一个 HTTP 请求，返回 (状态码, 解析后的 JSON 或原文)。"""
    payload = b""
    headers = [f"{method} {path} HTTP/1.1", "Host: localhost",
               "Accept: application/json", "Connection: close"]
    if body is not None:
        payload = json.dumps(body).encode()
        headers.append("Content-Type: application/json")
        headers.append(f"Content-Length: {len(payload)}")
    raw = ("\r\n".join(headers) + "\r\n\r\n").encode() + payload

    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with open(PIPE, "r+b", buffering=0) as pipe:
                pipe.write(raw)
                chunks: list[bytes] = []
                while True:
                    chunk = pipe.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    # 简单判断：读到完整响应体就停
                    data = b"".join(chunks)
                    if b"\r\n\r\n" in data and _body_complete(data):
                        break
            return _parse(b"".join(chunks))
        except OSError as exc:  # 管道忙，重试
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"连接命名管道失败: {last_error}")


def _body_complete(data: bytes) -> bool:
    head, _, body = data.partition(b"\r\n\r\n")
    lowered = head.lower()
    if b"transfer-encoding: chunked" in lowered:
        return body.endswith(b"0\r\n\r\n")
    for line in lowered.split(b"\r\n"):
        if line.startswith(b"content-length:"):
            try:
                return len(body) >= int(line.split(b":")[1].strip())
            except ValueError:
                return True
    return False


def _parse(data: bytes) -> tuple[int, Any]:
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1]) if len(lines[0].split()) > 1 else 0
    if b"transfer-encoding: chunked" in head.lower():
        body = _dechunk(body)
    text = body.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


def _dechunk(body: bytes) -> bytes:
    out = bytearray()
    while body:
        size_line, _, rest = body.partition(b"\r\n")
        try:
            size = int(size_line.strip(), 16)
        except ValueError:
            break
        if size == 0:
            break
        out += rest[:size]
        body = rest[size:].lstrip(b"\r\n")
    return bytes(out)


# -- 便捷封装 -------------------------------------------------------------
def get_proxies() -> dict[str, Any]:
    status, data = request("GET", "/proxies")
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"/proxies 返回异常: {status} {str(data)[:200]}")
    return data.get("proxies", {})


def selector_groups(proxies: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: info for name, info in proxies.items()
            if info.get("type") in ("Selector", "URLTest", "Fallback",
                                    "LoadBalance")}


def switch(group: str, node: str) -> None:
    import urllib.parse

    status, data = request("PUT", f"/proxies/{urllib.parse.quote(group)}",
                           {"name": node})
    if status not in (200, 204):
        raise RuntimeError(f"切换失败: {status} {str(data)[:200]}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    proxies = get_proxies()

    if cmd == "list":
        real = {n: i for n, i in proxies.items()
                if i.get("type") not in ("Selector", "URLTest", "Fallback",
                                         "LoadBalance", "Direct", "Reject",
                                         "Compatible", "Pass", "RejectDrop")}
        print(f"共 {len(real)} 个节点：")
        for name in real:
            print("  ", name)
    elif cmd == "groups":
        for name, info in selector_groups(proxies).items():
            print(f"[{info['type']}] {name}  当前={info.get('now')}  "
                  f"候选={len(info.get('all') or [])}")
    elif cmd == "switch" and len(sys.argv) >= 4:
        switch(sys.argv[2], sys.argv[3])
        print(f"已把 {sys.argv[2]} 切到 {sys.argv[3]}")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
