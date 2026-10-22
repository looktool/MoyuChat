"""UDP 广播自动发现服务器（局域网内免手输服务器 IP）。

原理：
  - 服务器端 ``run_responder_thread`` 在 UDP 端口监听，收到带 MAGIC 的探测包后，
    向发送方**单播**回包 {name, tcp_port, ws_port}。
  - 客户端 ``discover_sync`` 向广播地址（以及本机回环，便于同机/测试）发探测包，
    在超时时间内收集各服务器的回包，返回 [{ip, tcp_port, ws_port, name}, ...]。

注意：浏览器无法发送 UDP 广播，故本机制只对**桌面客户端**生效；
网页端仍通过 URL（http://<服务器IP>:5173）访问。
"""

import json
import os
import socket
import struct
import threading
import time

DISCOVERY_PORT = int(os.environ.get("MOYU_DISCOVERY_PORT", 9109))
MAGIC = b"MOYU_DISCOVER_V1"        # 客户端探测包内容
REPLY_MAGIC = b"MOYU_SERVER_V1"    # 服务器回包前缀
_MAX = 64                           # 最多收集多少台，防止被刷


def run_responder_thread(tcp_port, name="MoyuChat", ws_port=None,
                         port=DISCOVERY_PORT):
    """在后台线程启动 UDP 发现应答器（服务器调用一次即可）。

    绑定失败（端口被占用/无权限）时仅告警并返回，不影响服务器正常功能。
    """
    def loop():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("0.0.0.0", port))
        except OSError as e:
            print(f"[!] UDP 自动发现未启动（端口 {port} 可能被占用）：{e}")
            s.close()
            return
        payload = json.dumps(
            {"type": "moyu_server", "name": name,
             "tcp_port": tcp_port, "ws_port": ws_port},
            ensure_ascii=False).encode("utf-8")
        reply = REPLY_MAGIC + struct.pack(">H", len(payload)) + payload
        while True:
            try:
                data, addr = s.recvfrom(1024)
            except OSError:
                continue
            if data == MAGIC:
                try:
                    s.sendto(reply, addr)  # 单播回给探测方
                except OSError:
                    pass

    t = threading.Thread(target=loop, name="moyu-discovery", daemon=True)
    t.start()
    return t


def discover_sync(timeout=1.5, port=DISCOVERY_PORT):
    """阻塞式扫描局域网服务器，返回去重后的列表（按 IP）。线程内调用。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.bind(("0.0.0.0", 0))  # 随机本地端口
    except OSError:
        s.close()
        return []
    deadline = time.time() + timeout
    seen = {}
    for target in ("255.255.255.255", "127.0.0.1"):
        try:
            s.sendto(MAGIC, (target, port))
        except OSError:
            pass
    while time.time() < deadline and len(seen) < _MAX:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        s.settimeout(remaining)
        try:
            data, addr = s.recvfrom(2048)
        except socket.timeout:
            break
        except OSError:
            break
        if not data.startswith(REPLY_MAGIC):
            continue
        try:
            head = len(REPLY_MAGIC)
            n = struct.unpack(">H", data[head:head + 2])[0]
            info = json.loads(data[head + 2:head + 2 + n].decode("utf-8"))
        except Exception:
            continue
        ip = addr[0]
        if ip not in seen:
            seen[ip] = {"ip": ip, "tcp_port": info.get("tcp_port"),
                        "ws_port": info.get("ws_port"),
                        "name": info.get("name", "MoyuChat")}
    s.close()
    return list(seen.values())
