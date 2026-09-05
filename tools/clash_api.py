r"""通过 Windows 命名管道访问 mihomo 的外部控制 API。

Clash Verge 默认把 external-controller 关掉，只留 ``\\.\pipe\verge-mihomo``
命名管道。这里手写一个极简 HTTP over named pipe 客户端，用来：

* 列出可用节点和代理组
* 切换某个组的选中节点
* 测节点延迟

    python tools/clash_api.py list
    python tools/clash_api.py list --show-names
    python tools/clash_api.py groups
    python tools/clash_api.py groups --show-names
    python tools/clash_api.py switch <组名> <节点名>

``list`` / ``groups`` 默认只显示数量与类型等非名称信息。只有明确传入
``--show-names`` 时才显示代理组名、节点名或当前节点名。``switch`` 的输出
始终不回显传入名称。
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
        raise RuntimeError("读取代理信息失败")
    proxies = data.get("proxies")
    if not isinstance(proxies, dict):
        raise RuntimeError("代理信息格式异常")
    return proxies


def selector_groups(proxies: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: info for name, info in proxies.items()
            if isinstance(info, dict)
            and info.get("type") in ("Selector", "URLTest", "Fallback",
                                     "LoadBalance")}


def switch(group: str, node: str) -> None:
    import urllib.parse

    try:
        status, _ = request("PUT", f"/proxies/{urllib.parse.quote(group)}",
                            {"name": node})
    except Exception:  # noqa: BLE001
        raise RuntimeError("切换失败") from None
    if status not in (200, 204):
        raise RuntimeError("切换失败")


def _real_nodes(proxies: dict[str, Any]) -> dict[str, dict[str, Any]]:
    group_types = {
        "Selector", "URLTest", "Fallback", "LoadBalance", "Direct",
        "Reject", "Compatible", "Pass", "RejectDrop",
    }
    return {
        name: info
        for name, info in proxies.items()
        if isinstance(info, dict) and info.get("type") not in group_types
    }


def _show_names(args: list[str]) -> bool | None:
    if not args:
        return False
    if args == ["--show-names"]:
        return True
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args == ["--help"] or args == ["-h"]:
        print(__doc__)
        return 0 if args else 1
    cmd = args[0]

    if cmd == "list":
        show_names = _show_names(args[1:])
        if show_names is None:
            print(__doc__)
            return 1
        try:
            real = _real_nodes(get_proxies())
        except Exception:  # noqa: BLE001
            print("读取代理信息失败", file=sys.stderr)
            return 2
        ending = "：" if show_names else "。"
        print(f"共 {len(real)} 个节点{ending}")
        if show_names:
            for name in real:
                print("  ", name)
    elif cmd == "groups":
        show_names = _show_names(args[1:])
        if show_names is None:
            print(__doc__)
            return 1
        try:
            groups = selector_groups(get_proxies())
        except Exception:  # noqa: BLE001
            print("读取代理信息失败", file=sys.stderr)
            return 2
        print(f"共 {len(groups)} 个代理组：")
        for index, (name, info) in enumerate(groups.items(), start=1):
            candidates = len(info.get("all") or [])
            if show_names:
                print(f"[{info['type']}] {name}  当前={info.get('now')}  "
                      f"候选={candidates}")
            else:
                print(f"  组 {index} [{info['type']}] 候选={candidates}")
    elif cmd == "switch":
        if len(args) != 3:
            print("用法: python tools/clash_api.py switch <组名> <节点名>")
            return 1
        try:
            switch(args[1], args[2])
        except Exception:  # noqa: BLE001
            print("切换失败", file=sys.stderr)
            return 2
        print("切换成功")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
