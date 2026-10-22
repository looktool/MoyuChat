"""网页端生产托管：用纯标准库同时提供 ① 静态文件（web/dist）② WebSocket 桥接。

运行时无需 Node/Vite：先 `cd web && npm run build` 生成 web/dist，再运行本脚本，
浏览器打开 http://<局域网IP>:<端口> 即可用；/ws 由本进程直接桥接到 TCP 服务器。

启动：
    python serve_web.py
环境变量：
    MOYU_WEB_PORT        托管端口（默认 8080）
    MOYU_BACKEND_HOST/PORT  TCP 后端（默认 127.0.0.1:9000，与服务器一致）
"""

import asyncio
import base64
import hashlib
import os
import struct
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
# 打包成 exe（PyInstaller onefile）时，资源被解压到 sys._MEIPASS；web/dist 作为数据
# 一并打包，因此这里按“是否 frozen”选择基址，保证 exe 内也能找到前端静态文件。
_BASE = getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", ROOT) or ROOT
DIST = os.path.join(_BASE, "web", "dist")
BACKEND_HOST = os.environ.get("MOYU_BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("MOYU_BACKEND_PORT", "9000"))
HTTP_PORT = int(os.environ.get("MOYU_WEB_PORT", "8080"))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json", ".map": "application/json",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8", ".webp": "image/webp",
}


# ---------- WebSocket 帧（最小实现：text/close/ping/pong） ----------
async def read_frame(reader):
    hdr = await reader.readexactly(2)
    b0, b1 = hdr[0], hdr[1]
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    data = await reader.readexactly(length) if length else b""
    if masked:
        data = bytes(data[i] ^ mask[i % 4] for i in range(len(data)))
    return opcode, data


def build_frame(opcode, data=b""):
    b0 = 0x80 | opcode
    n = len(data)
    if n < 126:
        header = bytes([b0, n])
    elif n <= 0xFFFF:
        header = bytes([b0, 126]) + struct.pack(">H", n)
    else:
        header = bytes([b0, 127]) + struct.pack(">Q", n)
    return header + data


async def send_frame(writer, opcode, data=b""):
    writer.write(build_frame(opcode, data))
    await writer.drain()


# ---------- 静态文件 ----------
async def handle_http(writer, path):
    path = path.split("?", 1)[0]
    if path in ("/", ""):
        path = "/index.html"
    safe = os.path.normpath(path).lstrip("/\\")
    full = os.path.join(DIST, safe)
    if not os.path.abspath(full).startswith(os.path.abspath(DIST)):
        full = os.path.join(DIST, "index.html")  # 防目录穿越
    if not os.path.isfile(full):
        if os.path.splitext(full)[1]:  # 带扩展名的资源找不到 → 404
            body = b"Not Found"
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: %d\r\n"
                         b"Connection: close\r\n\r\n" % len(body) + body)
            await writer.drain()
            return
        full = os.path.join(DIST, "index.html")  # SPA 路由回退
    try:
        with open(full, "rb") as f:
            body = f.read()
    except OSError:
        body = b"Not Found"
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n" % len(body) + body)
        await writer.drain()
        return
    ctype = CONTENT_TYPES.get(os.path.splitext(full)[1].lower(),
                              "application/octet-stream")
    head = ("HTTP/1.1 200 OK\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
            "Connection: close\r\n\r\n" % (ctype, len(body))).encode()
    writer.write(head + body)
    await writer.drain()


# ---------- WebSocket -> TCP 桥接 ----------
async def handle_ws(reader, writer, header_lines):
    key = None
    for line in header_lines:
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    if not key:
        writer.close()
        return
    accept = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n"
                  % accept).encode())
    await writer.drain()

    try:
        breader, bwriter = await asyncio.open_connection(BACKEND_HOST, BACKEND_PORT)
    except OSError as e:
        await send_frame(writer, 1, ('{"type":"system","text":"无法连接后端：%s"}'
                                     % e).encode())
        writer.close()
        return

    async def ws_to_tcp():
        try:
            while True:
                opcode, data = await read_frame(reader)
                if opcode == 8:      # close
                    break
                if opcode == 9:      # ping -> pong
                    await send_frame(writer, 10, data)
                    continue
                if opcode == 1:      # text：原样加长度前缀转发给 TCP
                    bwriter.write(struct.pack(">I", len(data)) + data)
                    await bwriter.drain()
        except Exception:
            pass
        finally:
            try:
                bwriter.close()
            except Exception:
                pass

    async def tcp_to_ws():
        try:
            while True:
                hdr = await breader.readexactly(4)
                n = struct.unpack(">I", hdr)[0]
                body = await breader.readexactly(n) if n else b""
                await send_frame(writer, 1, body)
        except Exception:
            pass

    done, pending = await asyncio.wait(
        [asyncio.create_task(ws_to_tcp()), asyncio.create_task(tcp_to_ws())],
        return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    try:
        writer.close()
    except Exception:
        pass


# ---------- 连接入口：按请求头区分 静态 / WS ----------
async def handle(reader, writer):
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = await reader.read(4096)
            if not chunk:
                writer.close()
                return
            data += chunk
        head = data.split(b"\r\n\r\n", 1)[0].decode("latin1")
        lines = head.split("\r\n")
        parts = lines[0].split(" ")
        path = parts[1] if len(parts) > 1 else "/"
        if "upgrade: websocket" in head.lower():
            await handle_ws(reader, writer, lines[1:])
        else:
            await handle_http(writer, path)
    except Exception:
        try:
            writer.close()
        except Exception:
            pass


async def start_web(port):
    """启动静态托管 + WS 桥接服务器，返回 asyncio Server（供 server_app 复用）。

    找不到 web/dist 时返回 None（仅提示，不影响 TCP/发现）。
    """
    if not os.path.isfile(os.path.join(DIST, "index.html")):
        print("[!] 未找到 web/dist/index.html，网页端不可用"
              "（不影响 TCP 服务器与自动发现）；构建：cd web && npm run build")
        return None
    return await asyncio.start_server(handle, "0.0.0.0", port)


async def main():
    srv = await start_web(HTTP_PORT)
    if srv is None:
        return
    sys.path.insert(0, ROOT)
    from common import lan_ip
    print(f"网页端已托管：http://{lan_ip()}:{HTTP_PORT}")
    print(f"  （内置 WS 桥接 -> TCP {BACKEND_HOST}:{BACKEND_PORT}，无需 Node）")
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已停止")
