"""GUI 端到端集成测试：真实 Tk 客户端 + 真实服务器进程。

覆盖“点登录→建主界面→收发消息→未读/已读→撤回”整条链路，
补足 test_ui.py（纯桩件）与 test_full.py（纯协议）之间的空白。

用法：python test_e2e.py
"""

import os
import socket
import subprocess
import sys
import tempfile
import time

PORT = 9103
STORE = os.path.join(tempfile.gettempdir(), "moyu_e2e_store.json")
os.environ["MOYU_PORT"] = str(PORT)
os.environ["MOYU_STORE"] = STORE

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk  # noqa: E402

import client as client_mod  # noqa: E402
from common import HOST, send_obj, recv_obj  # noqa: E402

PASS = 0


class _NoDialog:
    """消息弹窗桩件：弹窗是模态的，会卡住测试的事件循环。"""

    calls = []

    def _record(self, kind, *a, **kw):
        _NoDialog.calls.append((kind, a))

    showinfo = lambda self, *a, **kw: self._record("info", *a, **kw)
    showwarning = lambda self, *a, **kw: self._record("warning", *a, **kw)
    showerror = lambda self, *a, **kw: self._record("error", *a, **kw)
    askyesno = lambda self, *a, **kw: True


client_mod.messagebox = _NoDialog()   # 必须在建界面之前打好桩


def assert_(cond, msg):
    global PASS
    if not cond:
        raise AssertionError("❌ " + msg)
    PASS += 1
    print("  ✓", msg)


def pump(root, app, seconds=3.0, until=None):
    """驱动 Tk 事件循环，直到条件成立或超时。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        root.update()
        if until is not None and until():
            return True
        time.sleep(0.03)
    return False if until is not None else True


def peer_connect(name):
    s = socket.create_connection((HOST, PORT), timeout=5)
    s.settimeout(3)
    send_obj(s, {"type": "login", "username": name})
    return s


def drain(sock, seconds=0.6):
    out = []
    sock.settimeout(0.2)
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            m = recv_obj(sock)
        except socket.timeout:
            continue
        except Exception:
            break
        if m is None:
            break
        out.append(m)
    return out


def main():
    for p in (STORE, STORE + ".corrupt"):
        if os.path.exists(p):
            os.remove(p)
    proc = subprocess.Popen([sys.executable, "server.py"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.STDOUT)
    try:
        time.sleep(1.5)
        root = tk.Tk()
        app = client_mod.ChatClient(root)
        app.name_var.set("tanuki")

        print("[1] 点击登录 → 建立主界面")
        app.do_connect()
        ok = pump(root, app, 6.0, lambda: app.main_built)
        assert_(ok, "登录成功后应自动建立主界面")
        assert_(root.title().endswith("tanuki"), f"标题未更新: {root.title()}")

        print("[2] 收到私聊消息 → 进入会话并累计未读")
        peer = peer_connect("partner")
        drain(peer, 0.4)
        time.sleep(0.3)
        pump(root, app, 1.5, lambda: "partner" in app.conv_type)
        assert_("partner" in app.conv_type, "对端上线后应出现在会话列表")
        send_obj(peer, {"type": "chat", "to": "tanuki", "text": "你好呀"})
        ok = pump(root, app, 4.0,
                  lambda: any(e["text"] == "你好呀"
                              for e in app.buffers.get("partner", [])))
        assert_(ok, "客户端应收到并缓存私聊消息")
        assert_(app.unread.get("partner", 0) == 1,
                f"未打开会话应累计未读: {app.unread}")

        print("[3] 打开会话 → 自动回执已读 → 对方收到 receipt")
        app.select_conv("partner")
        pump(root, app, 1.5)
        assert_(app.unread.get("partner", 0) == 0, "打开会话后未读应清零")
        got = drain(peer, 1.0)
        assert_(any(m.get("type") == "read_receipt" for m in got),
                f"对端应收到已读回执: {got}")

        print("[4] 界面发送消息 → 对方收到")
        app.input.delete("1.0", tk.END)
        app.input.insert("1.0", "收到啦")
        app.send_msg()
        pump(root, app, 0.3)
        got = drain(peer, 1.5)
        assert_(any(m.get("type") == "private" and m.get("text") == "收到啦"
                    for m in got), f"对端应收到界面发出的消息: {got}")

        print("[5] 收到对方已读 → 我方消息变双勾")
        mid = next((m["id"] for m in got
                    if m.get("type") == "private" and m["text"] == "收到啦"),
                   None)
        assert_(mid is not None, "应能从回显/转发中拿到消息 id")
        # 对端视角的会话名是 "partner"，服务端须换算成我方视角的 "partner"
        send_obj(peer, {"type": "read", "target": "partner",
                        "id": mid, "to": "tanuki"})
        pump(root, app, 1.5)
        mine = [e for e in app.buffers["partner"] if e.get("id") == mid]
        assert_(mine and mine[0].get("status") == "read",
                f"应更新为已读双勾: {mine}")

        print("[6] 撤回：界面发出的消息可撤回，双方同步为占位")
        app.recall_message(mine[0])
        pump(root, app, 1.5, lambda: mine[0].get("recalled") is True)
        assert_(mine[0].get("recalled") is True, "撤回后本地应标记为已撤回")
        assert_(app.preview_of("partner") == "消息已撤回",
                f"列表预览应为已撤回: {app.preview_of('partner')}")
        got = drain(peer, 1.0)
        rc = next((m for m in got if m.get("type") == "recalled"
                   and m.get("id") == mid), None)
        assert_(rc is not None, f"对端应收到撤回通知: {got}")
        assert_(rc["conv"] == "tanuki",
                f"对端视角的会话名应是 tanuki（否则对端界面不刷新）: {rc}")

        print("[7] 群聊：建群 → 群消息 → 界面可见")
        app.input.delete("1.0", tk.END)
        app.try_send({"type": "group_create", "group": "e2e组",
                      "members": ["partner"]})
        ok = pump(root, app, 4.0, lambda: "e2e组" in app.conv_type)
        assert_(ok, "建群后会话列表应出现该群")
        pump(root, app, 1.0, lambda: "partner" in app.group_members.get("e2e组", []))
        assert_("partner" in app.group_members.get("e2e组", []),
                f"群成员应包含 partner: {app.group_members.get('e2e组')}")
        send_obj(peer, {"type": "group_msg", "group": "e2e组", "text": "群里好"})
        ok = pump(root, app, 4.0,
                  lambda: any(e["text"] == "群里好"
                              for e in app.buffers.get("e2e组", [])))
        assert_(ok, "群消息应进入本地缓存")
        app.select_conv("e2e组")
        pump(root, app, 0.6)
        assert_(app.active == "e2e组" and app.active_type == "group",
                "应能选中群会话")

        print("[8] 断线 → 自动重连 → 仍能通信")
        inner_before = app.conv_inner
        dead_sock = app.sock
        relogins = {"n": 0}                # 统计重连后服务端下发的 user_list
        real_handle = app.handle_msg

        def counting_handle(m):
            if m.get("type") == "user_list":
                relogins["n"] += 1
            real_handle(m)

        app.handle_msg = counting_handle
        dead_sock.close()                  # 模拟网络中断（socket 被对端/系统断开）
        ok = pump(root, app, 12.0,
                  lambda: app.sock is not None and app.sock is not dead_sock
                  and app.reconnecting is False and relogins["n"] > 0)
        app.handle_msg = real_handle
        assert_(ok, "断线后应自动重连并重新完成登录")
        assert_(app.main_built and app.conv_inner is inner_before,
                "重连应复用原界面、不重建")
        assert_(app.buffers.get("partner"), "重连后本地聊天记录应保留")
        # 重连后的会话必须已注册，能正常收发
        send_obj(peer, {"type": "chat", "to": "tanuki", "text": "重连后还在吗"})
        ok = pump(root, app, 4.0,
                  lambda: any(e["text"] == "重连后还在吗"
                              for e in app.buffers.get("partner", [])))
        assert_(ok, "重连后仍能收到私聊消息")

        peer.close()

        print("[9] 同名登录被顶下线：不再自动重连（避免双方互踢）")
        # 用原始连接冒充“另一个窗口”登录同一账号，服务端应顶掉 Tk 客户端
        hijack = peer_connect("tanuki")
        got = drain(hijack, 1.0)
        assert_(any(m.get("type") == "user_list" for m in got),
                "新会话应登录成功并接管")
        ok = pump(root, app, 4.0, lambda: app.kicked)
        assert_(ok, "被顶下线的客户端应收到 kicked 通知")
        pump(root, app, 2.0)   # 再等一会儿，确认它没有偷偷重连互踢
        assert_(app.reconnecting is False and app.sock is None,
                f"被顶下线后不应自动重连: reconnecting={app.reconnecting}")
        assert_(app.main_built is False, "被顶下线后应回到登录界面")
        # 原会话已经不在服务端注册，消息应发给新会话
        send_obj(hijack, {"type": "list_users"})
        lu = drain(hijack, 0.6)
        assert_(any(m.get("type") == "user_list" for m in lu),
                "接管后的新会话可正常通信")
        hijack.close()

        root.destroy()
        print(f"\n✅ GUI 端到端测试全部通过（{PASS} 项断言）")
    finally:
        proc.terminate()
        proc.wait()
        for p in (STORE, STORE + ".corrupt"):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


if __name__ == "__main__":
    main()
