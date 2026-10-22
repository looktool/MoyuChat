"""服务器间联邦（跨服务器通信）端到端测试。

启动两台独立的 MoyuChat 服务器 A/B，互设为对等，验证：
  1) 跨服务器私聊双向投递（A 上的 alice <-> B 上的 carol）；
  2) 跨服务器发现（A 上搜索 carol 得到 remote 结果）；
  3) 已读回执跨服务器同步（carol 读后 alice 收到 read_receipt）；
  4) 容错：对端宕机时发送方收到“不可达”提示；对端恢复后自动重连、消息恢复投递。

运行：python test_fed.py   （用独立 JSON 存储，不依赖 MySQL）
"""

import json
import os
import socket
import struct
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


# ---------------- 原始 TCP 客户端（复刻服务器帧格式） ----------------
class Conn:
    def __init__(self, host, port):
        self.s = socket.create_connection((host, port), timeout=8)
        self.s.settimeout(8)
        self.buf = b""

    def send(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.s.sendall(len(data).to_bytes(4, "big") + data)

    def read(self, timeout=8):
        self.s.settimeout(timeout)
        while True:
            if len(self.buf) >= 4:
                n = int.from_bytes(self.buf[:4], "big")
                if len(self.buf) >= 4 + n:
                    frame = self.buf[4:4 + n]
                    self.buf = self.buf[4 + n:]
                    return json.loads(frame.decode())
            try:
                chunk = self.s.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self.buf += chunk

    def login(self, name):
        self.send({"type": "login", "username": name})
        while True:
            m = self.read()
            if m is None:
                raise RuntimeError("login 超时")
            if m.get("type") == "login_ok":
                return
            # 吞掉握手消息（user_list/group_list/presence...）

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def start_server(label, port, fed_port, peers, discovery_port):
    env = dict(os.environ)
    env.update({
        "MOYU_STORAGE": "json",
        "MOYU_STORE": os.path.join(ROOT, "data", f"fed_{label}.json"),
        "MOYU_PORT": str(port),
        "MOYU_FED_PORT": str(fed_port),
        "MOYU_SERVER_ID": label,
        "MOYU_PEERS": peers,
        "MOYU_DISCOVERY_PORT": str(discovery_port),
    })
    log = open(os.path.join(ROOT, f"fed_{label}.log"), "w", encoding="utf-8")
    p = subprocess.Popen([sys.executable, "-u", "server.py"], env=env,
                         cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    return p


def wait_line(sub, label, text, timeout=20):
    """等待服务器日志出现某行（联邦链路建立）。"""
    path = os.path.join(ROOT, f"fed_{label}.log")
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                if any(text in ln for ln in f.read().splitlines()):
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def main():
    # A: client 9100 / fed 9101；B: client 9102 / fed 9103；互相拨号
    pa = start_server("A", 9100, 9101, "127.0.0.1:9103", 9119)
    pb = start_server("B", 9102, 9103, "127.0.0.1:9101", 9129)
    try:
        assert wait_line(pa, "A", "[fed] 已与对端", 25), "A 未与 B 建立联邦"
        assert wait_line(pb, "B", "[fed] 已与对端", 25), "B 未与 A 建立联邦"
        print("PASS 两台服务器建立联邦链路")

        # 登录：alice/bob 在 A，carol 在 B
        a = Conn("127.0.0.1", 9100); a.login("alice")
        b = Conn("127.0.0.1", 9100); b.login("bob")
        c = Conn("127.0.0.1", 9102); c.login("carol")
        time.sleep(0.5)

        # ---- 1) 跨服务器私聊：alice(A) -> carol(B) ----
        a.send({"type": "chat", "to": "carol", "text": "你好，我是 A 上的 alice"})
        got = None
        for _ in range(20):
            m = c.read(1)
            if m and m.get("type") == "private" and m.get("from") == "alice":
                got = m; break
        assert got, "carol 未收到来自 alice 的跨服务器消息"
        mid = got["id"]
        print("PASS 跨服务器私聊投递（alice->carol）")

        # carol(B) -> alice(A) 反向
        c.send({"type": "chat", "to": "alice", "text": "收到，我是 B 上的 carol"})
        got2 = None
        for _ in range(20):
            m = a.read(1)
            if m and m.get("type") == "private" and m.get("from") == "carol":
                got2 = m; break
        assert got2, "alice 未收到来自 carol 的跨服务器消息"
        print("PASS 跨服务器私聊投递（carol->alice，双向）")

        # ---- 2) 跨服务器发现 ----
        a.send({"type": "query_user", "user": "carol"})
        res = None
        for _ in range(20):
            m = a.read(1)
            if m and m.get("type") == "search_result":
                res = m; break
        assert res and any(u.get("name") == "carol" and u.get("remote")
                           for u in res.get("users", [])), "未发现远程用户 carol"
        print("PASS 跨服务器发现（search_result 含 remote 用户）")
        # 本地用户仍标记 remote=False
        a.send({"type": "query_user", "user": "bob"})
        res2 = None
        for _ in range(20):
            m = a.read(1)
            if m and m.get("type") == "search_result":
                res2 = m; break
        assert res2 and any(u.get("name") == "bob" and not u.get("remote")
                            for u in res2.get("users", [])), "本地用户应标记 remote=False"
        print("PASS 发现区分本地/远程用户")

        # ---- 3) 已读回执跨服务器同步 ----
        c.send({"type": "read", "conv": "alice", "id": mid})
        rr = None
        for _ in range(20):
            m = a.read(1)
            if m and m.get("type") == "read_receipt" and m.get("target") == "carol":
                rr = m; break
        assert rr, "alice 未收到跨服务器的已读回执"
        print("PASS 已读回执跨服务器同步")

        # ---- 4) 容错：对端宕机 -> 明确不可达；恢复 -> 自动重连投递 ----
        pb.terminate(); pb.wait()
        time.sleep(1.0)
        a.send({"type": "chat", "to": "carol", "text": "对端宕机时发的消息"})
        sys_note = None
        for _ in range(20):
            m = a.read(1)
            if m and m.get("type") == "system" and m.get("conv") == "carol":
                sys_note = m; break
        assert sys_note and "不可达" in sys_note.get("text", ""), \
            "对端宕机时应提示不可达，实际: %s" % sys_note
        print("PASS 对端宕机时发送方收到“不可达”提示（优雅降级）")

        # 恢复 B
        pb = start_server("B", 9102, 9103, "127.0.0.1:9101", 9129)
        assert wait_line(pb, "B", "[fed] 已与对端", 25), "B 恢复后未重连联邦"
        time.sleep(0.5)
        c2 = Conn("127.0.0.1", 9102); c2.login("carol")
        # 恢复后正常投递
        a.send({"type": "chat", "to": "carol", "text": "恢复后的消息"})
        got3 = None
        for _ in range(20):
            m = c2.read(1)
            if m and m.get("type") == "private" and m.get("from") == "alice" \
                    and m.get("text") == "恢复后的消息":
                got3 = m; break
        assert got3, "B 恢复后跨服务器消息未恢复投递"
        print("PASS 对端恢复后自动重连并恢复投递")

        a.close(); b.close(); c.close(); c2.close()
        print("\n✅ 联邦（跨服务器通信）全部测试通过")
        sys.exit(0)
    finally:
        for p in (pa, pb):
            try:
                p.terminate()
            except Exception:
                pass
        # 清理测试存储
        for f in ("fed_A.json", "fed_B.json"):
            try:
                os.remove(os.path.join(ROOT, "data", f))
            except Exception:
                pass


if __name__ == "__main__":
    main()
