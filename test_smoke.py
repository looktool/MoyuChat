"""冒烟测试：自拉起服务器子进程，验证登录/私聊/群聊/回执/离线消息。"""
import socket
import subprocess
import sys
import time

from common import send_obj, recv_obj, HOST, PORT


def connect(name):
    s = socket.create_connection((HOST, PORT), timeout=5)
    send_obj(s, {"type": "login", "username": name})
    while True:
        m = recv_obj(s)
        if m and m.get("type") == "login_ok":
            return s
        if m is None:
            raise RuntimeError(f"{name} 登录失败")


def collect(s, wait=0.4):
    s.settimeout(0.3)
    out = []
    end = time.time() + wait
    while time.time() < end:
        try:
            m = recv_obj(s)
            if m:
                out.append(m)
        except socket.timeout:
            break
    return out


def main():
    # 若端口已被占用（例如你正在运行的服务器），则复用；否则自拉起
    proc = None
    try:
        probe = socket.create_connection((HOST, PORT), timeout=1)
        probe.close()
    except Exception:
        proc = subprocess.Popen([sys.executable, "server.py"])
        time.sleep(1.5)
    try:
        a = connect("alice")
        b = connect("bob")

        # 1) bob 应收到含 alice 的在线列表
        msgs = collect(b)
        assert any(m.get("type") == "user_list" and "alice" in m["users"]
                   for m in msgs), "bob 未收到含 alice 的在线列表"

        # 2) alice 私聊 bob（含 id）
        send_obj(a, {"type": "chat", "to": "bob", "text": "你好 bob"})
        mb = collect(b)
        priv = next((m for m in mb if m.get("type") == "private"
                     and m["from"] == "alice"
                     and m["text"] == "你好 bob"), None)
        assert priv is not None, "bob 未收到私聊"
        assert "id" in priv, "私聊消息缺少 id（无法打勾）"
        ma = collect(a)
        assert any(m.get("type") == "private" and m.get("id") == priv["id"]
                   for m in ma), "alice 未收到发送回执(单勾)"

        # 2.1) bob 已读 -> alice 收到已读回执(双勾)
        send_obj(b, {"type": "read", "target": "alice",
                     "id": priv["id"], "to": "alice"})
        ma = collect(a)
        assert any(m.get("type") == "read_receipt"
                   and m.get("id") == priv["id"]
                   for m in ma), "alice 未收到已读回执(双勾)"

        # 3) alice 建群并邀请 bob
        send_obj(a, {"type": "group_create", "group": "dev",
                     "members": ["bob"]})
        collect(a, 0.3)
        send_obj(b, {"type": "group_join", "group": "dev"})
        mj = collect(b, 0.3)
        assert any(m.get("type") == "group_info" and m["group"] == "dev"
                   and "alice" in m["members"] and "bob" in m["members"]
                   for m in mj), "bob 未收到群成员信息"

        # 4) alice 发群消息
        send_obj(a, {"type": "group_msg", "group": "dev", "text": "大家好"})
        mb = collect(b)
        assert any(m.get("type") == "group" and m["group"] == "dev"
                   and m["text"] == "大家好" for m in mb), "bob 未收到群消息"

        # 4.1) “正在输入”转发：alice 输入时 bob 应收到 typing 事件
        send_obj(a, {"type": "typing", "conv": "bob"})
        mb = collect(b, 0.4)
        assert any(m.get("type") == "typing" and m["from"] == "alice"
                   for m in mb), "bob 未收到 typing 事件"

        # 5) 离线消息：amy 发给离线 zoe，zoe 上线后应收到的离线消息
        amy = connect("amy")
        send_obj(amy, {"type": "chat", "to": "zoe", "text": "离线留言"})
        collect(amy, 0.3)
        zoe = connect("zoe")
        mz = collect(zoe, 0.6)
        assert any(m.get("type") == "private" and m["from"] == "amy"
                   and m["text"] == "离线留言" for m in mz), \
            "zoe 未收到离线消息"

        print("✅ 冒烟测试全部通过：登录/私聊/群聊/单双勾回执/离线消息")
        a.close(); b.close(); amy.close(); zoe.close()
    finally:
        if proc:
            proc.terminate()
            proc.wait()


if __name__ == "__main__":
    main()
