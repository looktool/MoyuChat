"""MoyuChat 客户端/服务器共享的 TCP 通信工具。

通信协议（基于 TCP/IP）：
  每个报文 = 4 字节大端长度前缀 + UTF-8 编码的 JSON 字符串。
  长度前缀保证接收方能完整切分出一个 JSON 对象，避免粘包/半包问题。
"""

import socket
import json
import os

# 客户端默认连接本机；如需跨机通信，改为服务器 IP 即可。
# 可通过环境变量覆盖，便于并行测试/隔离（不影响默认行为）。
HOST = os.environ.get("MOYU_HOST", "127.0.0.1")
# 服务器监听端口
PORT = int(os.environ.get("MOYU_PORT", 9000))
# 服务器绑定地址（默认监听所有网卡）
BIND_HOST = os.environ.get("MOYU_BIND_HOST", "0.0.0.0")

# 单帧最大字节数（16MB）：防御异常/恶意的超长长度前缀导致内存暴涨
MAX_FRAME = int(os.environ.get("MOYU_MAX_FRAME", 16 * 1024 * 1024))

# 单条消息最大字符数（仿 Telegram 的 4096 上限）：防止超长文本刷爆内存/磁盘
MAX_TEXT = int(os.environ.get("MOYU_MAX_TEXT", 4096))

# 用户名/群名长度上限
MAX_NAME = int(os.environ.get("MOYU_MAX_NAME", 24))

# 消息可撤回的时间窗（秒）：与服务端保持一致，客户端据此决定是否显示“撤回”
RECALL_WINDOW = int(os.environ.get("MOYU_RECALL_WINDOW", 120))

# 应用层心跳：客户端每 PING_INTERVAL 秒发一次 ping，超过 PING_TIMEOUT 秒没收到
# pong 就认为链路已死并主动重连（TCP keepalive 默认两小时才发现半开连接）
PING_INTERVAL = int(os.environ.get("MOYU_PING_INTERVAL", 30))
PING_TIMEOUT = int(os.environ.get("MOYU_PING_TIMEOUT", 100))


def lan_ip():
    """探测本机局域网 IP（局域网多人聊天时用于展示/默认连接地址）。

    用一个 UDP socket 向公网地址 connect（并不真正发包），借内核路由表选出
    本机出口网卡的 IP；无可用网卡/路由时回退 127.0.0.1。无需联网即可工作。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def valid_name(name):
    """校验用户名/群名：长度受限且不含控制字符。

    控制字符一律拒绝：私聊会话键用 \\x1f 分隔，若名字里也能出现该字符会
    导致不同会话映射到同一存储键；换行等字符还会破坏聊天界面显示。
    """
    if not name or len(name) > MAX_NAME:
        return False
    return all(ch.isprintable() for ch in name)


def send_obj(sock, obj):
    """将一个 Python 对象（dict）序列化为 JSON 并通过 TCP 发送。"""
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ValueError("报文超过最大长度限制")
    sock.sendall(len(data).to_bytes(4, "big") + data)


def recv_obj(sock):
    """从 TCP 连接接收一个完整 JSON 报文，返回 dict；异常/关闭时返回 None。"""
    header = _recvn(sock, 4)
    if not header:
        return None
    length = int.from_bytes(header, "big")
    if length <= 0 or length > MAX_FRAME:  # 非法长度，视为连接不可信
        return None
    payload = _recvn(sock, length)
    if not payload:
        return None
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):  # 坏帧：不抛异常，交由调用方处理
        return None
    return obj


def _recvn(sock, n):
    """精确读取 n 字节，不足则循环补齐；对端关闭时返回 None。"""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf
