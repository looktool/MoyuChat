"""MoyuChat 一体化服务器（一键在局域网起服）。

一个进程同时提供：
  - TCP 聊天服务器（默认 0.0.0.0:9000）
  - UDP 广播自动发现（默认端口 9109，桌面客户端免手输 IP）
  - 网页端静态托管 + 内置 WS 桥接（默认 8080，运行时无需 Node；需先构建 web/dist）

存储：默认 JSON（零依赖，开箱即用）；设置 MOYU_STORAGE=mysql 可改用 MySQL
（需本机有 MySQL 并 pip install PyMySQL）。构建单文件 exe 见 MoyuChatServer.spec。

用法：
    python server_app.py
    MOYU_STORAGE=mysql python server_app.py
    # 端口：MOYU_PORT / MOYU_WEB_PORT / MOYU_DISCOVERY_PORT
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import discover  # noqa: E402
import serve_web  # noqa: E402
import server as server_mod  # noqa: E402
from common import BIND_HOST, PORT, lan_ip  # noqa: E402


async def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)  # 控制台横幅即时可见（含重定向到文件）
    except Exception:
        pass
    chat = server_mod.ChatServer()
    tcp = await asyncio.start_server(chat.handle, BIND_HOST, PORT)
    asyncio.create_task(chat.sweep_dead())  # 清理无心跳死连接

    web_port = int(os.environ.get("MOYU_WEB_PORT", "8080"))
    ws_port = int(os.environ.get("MOYU_WS_PORT", "9106"))
    discover.run_responder_thread(PORT, name="MoyuChat", ws_port=ws_port)

    ip = lan_ip()
    store_kind = os.environ.get("MOYU_STORAGE", "json").lower()
    print("=" * 58)
    print(" MoyuChat 一体化服务器已启动")
    print("=" * 58)
    print(f" 本机局域网 IP：{ip}")
    print(f" 聊天服务器：{ip}:{PORT}   （桌面客户端登录框填此 IP，或自动发现）")
    print(f" 网页端：http://{ip}:{web_port}   （需先构建 web/dist）")
    print(f" UDP 自动发现：端口 {discover.DISCOVERY_PORT}")
    print(f" 存储：{store_kind}")
    print(" 按 Ctrl+C 停止")
    print("=" * 58)

    web = await serve_web.start_web(web_port)
    servers = [tcp] + ([web] if web else [])
    async with _multi(servers):
        await asyncio.Event().wait()


class _multi:
    """把多个 asyncio Server 作为一组 async context manager 管理。"""

    def __init__(self, servers):
        self.servers = servers

    async def __aenter__(self):
        for s in self.servers:
            await s.__aenter__()
        return self

    async def __aexit__(self, *exc):
        for s in self.servers:
            await s.__aexit__(*exc)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n服务器已停止")
