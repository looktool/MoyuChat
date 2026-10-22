"""WebSocket 桥接：让浏览器（无法建裸 TCP）连到现有 MoyuChat TCP 服务器。

浏览器连本桥（默认 ws://127.0.0.1:9106/ws），本桥为每个浏览器连接建立一个到
Python 服务器的 TCP 连接，做「JSON 文本帧」与「4 字节长度前缀 + JSON」TCP 帧
的双向翻译。登录由浏览器先发 {type:'login', username}，本桥原样转发给 TCP 后端。

启动：
    pip install websockets
    python ws_bridge.py

环境变量：
    MOYU_BACKEND_HOST / MOYU_BACKEND_PORT  TCP 后端地址（默认 127.0.0.1:9100）
    MOYU_WS_HOST / MOYU_WS_PORT            桥接监听地址（默认 0.0.0.0:9106）
"""

import asyncio
import json
import os
import struct

import websockets

BACKEND_HOST = os.environ.get("MOYU_BACKEND_HOST", "127.0.0.1")
# 默认与服务器 common.py 的 PORT（9000）一致，保证 `python server.py` + `python ws_bridge.py` 开箱即用
BACKEND_PORT = int(os.environ.get("MOYU_BACKEND_PORT", "9000"))
WS_HOST = os.environ.get("MOYU_WS_HOST", "0.0.0.0")
WS_PORT = int(os.environ.get("MOYU_WS_PORT", "9106"))


def _frame(obj: dict) -> bytes:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return struct.pack(">I", len(data)) + data


async def _read_obj(reader) -> dict:
    hdr = await reader.readexactly(4)
    n = struct.unpack(">I", hdr)[0]
    body = await reader.readexactly(n)
    return json.loads(body.decode("utf-8"))


async def _pump_ws_to_tcp(ws, writer):
    """浏览器 -> TCP 后端（JSON 文本 -> 长度前缀帧）"""
    async for data in ws:
        try:
            msg = json.loads(data)
        except Exception:
            continue
        try:
            writer.write(_frame(msg))
            await writer.drain()
        except Exception:
            break


async def _pump_tcp_to_ws(reader, ws):
    """TCP 后端 -> 浏览器（长度前缀帧 -> JSON 文本）"""
    try:
        while True:
            msg = await _read_obj(reader)
            await ws.send(json.dumps(msg, ensure_ascii=False))
    except Exception:
        pass


async def handler(ws):
    try:
        reader, writer = await asyncio.open_connection(BACKEND_HOST, BACKEND_PORT)
    except Exception as e:
        try:
            await ws.send(json.dumps({"type": "system",
                                      "text": f"无法连接后端：{e}"}))
        except Exception:
            pass
        return

    tasks = [
        asyncio.create_task(_pump_ws_to_tcp(ws, writer)),
        asyncio.create_task(_pump_tcp_to_ws(reader, ws)),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


async def main():
    async with websockets.serve(handler, WS_HOST, WS_PORT):
        print(f"WebSocket 桥接已启动：ws://{WS_HOST}:{WS_PORT}/ws  "
              f"-> TCP {BACKEND_HOST}:{BACKEND_PORT}")
        try:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from common import lan_ip
            print(f"局域网网页端访问：http://{lan_ip()}:5173"
                  f"（其他人用该地址打开即可聊天）")
        except Exception:
            pass
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
