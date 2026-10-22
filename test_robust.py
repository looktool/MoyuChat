"""健壮性 / 抗压测试：向服务器发送各种畸形报文，验证服务器不会崩溃。

覆盖：
  1. 非法 JSON 帧（坏帧）应被忽略，连接仍可继续正常通信；
  2. 超长长度前缀（声称 4GB）应被拒绝并断开该连接；
  3. 建立连接后立即关闭、只发半截帧等异常行为，服务器应存活；
  4. 空用户名、超长用户名、非字符串字段等非法登录，服务器应正确拒绝；
  5. 未登录即发消息、非群成员发群消息等越权操作应被忽略；
  6. 超长单条消息应被拒绝（回系统提示且不投递、不落库）；
  7. 以上之后，服务器仍能正常服务新用户（证明未崩溃）。

独立端口 9101 运行，不影响其它测试与正在运行的服务器。
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("MOYU_PORT", "9101")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import HOST, PORT, MAX_TEXT, send_obj, recv_obj  # noqa: E402

client_max = MAX_TEXT  # 与服务端一致的消息长度上限
RATE_LIMIT = 1500      # 与 server.py 的限流阈值一致（触发限流用）

PASS = 0


def assert_(cond, msg):
    global PASS
    if not cond:
        raise AssertionError("❌ " + msg)
    PASS += 1
    print("  ✓", msg)


def connect(name):
    s = socket.create_connection((HOST, PORT), timeout=5)
    s.settimeout(3)
    send_obj(s, {"type": "login", "username": name})
    obj = recv_obj(s)
    assert_(obj and obj.get("type") == "login_ok", f"{name} 登录成功")
    return s


def drain(s, timeout=0.5):
    """收集一段时间内的所有报文，返回列表。"""
    out = []
    s.settimeout(timeout)
    while True:
        try:
            obj = recv_obj(s)
        except (socket.timeout, OSError):
            break
        if obj is None:
            break
        out.append(obj)
    return out


def raw_send(s, payload: bytes):
    s.sendall(len(payload).to_bytes(4, "big") + payload)


def main():
    store_path = os.path.join(tempfile.gettempdir(), "moyu_robust_store.json")
    if os.path.exists(store_path):
        os.remove(store_path)  # 每次从干净状态开始，保证用例可重复运行
    env = dict(os.environ, MOYU_PORT=str(PORT), MOYU_STORE=store_path)
    proc = subprocess.Popen([sys.executable, "server.py"], env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.STDOUT)
    time.sleep(1.5)
    try:
        print("[1] 坏帧（非法 JSON）应被忽略，连接仍可继续使用")
        s = connect("fuzz1")
        raw_send(s, b"{not valid json")          # 坏帧
        raw_send(s, json.dumps([1, 2, 3]).encode())  # 非 dict
        send_obj(s, {"type": "list_users"})      # 正常请求
        msgs = drain(s, 0.6)
        assert_(any(m.get("type") == "user_list" for m in msgs),
                "坏帧之后的正常请求仍被处理（服务器未因此断开/崩溃）")

        print("[2] 超长长度前缀应被拒绝并断开该连接")
        bad = socket.create_connection((HOST, PORT), timeout=5)
        bad.sendall((0xFFFFFFFF).to_bytes(4, "big"))  # 声称 4GB
        bad.settimeout(1.5)
        closed = False
        try:
            closed = (bad.recv(1) == b"")
        except Exception:
            closed = True
        assert_(closed, "非法长度前缀的连接被服务器断开")
        bad.close()

        print("[3] 半截帧 / 立即关闭等异常行为不应影响服务器")
        half = socket.create_connection((HOST, PORT), timeout=5)
        half.sendall(b"\x00\x00")                # 只发了一半的长度前缀
        half.close()
        conn_close = socket.create_connection((HOST, PORT), timeout=5)
        conn_close.close()                       # 连上就关
        time.sleep(0.3)

        print("[4] 非法登录应被拒绝")
        s2 = socket.create_connection((HOST, PORT), timeout=5)
        s2.settimeout(3)
        send_obj(s2, {"type": "login", "username": ""})       # 空用户名
        r = recv_obj(s2)
        assert_(r and r.get("type") == "login_fail", "空用户名被拒绝")
        s2.close()

        s3 = socket.create_connection((HOST, PORT), timeout=5)
        s3.settimeout(3)
        send_obj(s3, {"type": "login", "username": ["x"]})    # 非字符串
        r3 = recv_obj(s3)
        assert_(r3 is None or r3.get("type") == "login_fail",
                "非字符串用户名被拒绝或忽略")
        s3.close()

        print("[4.5] 非法用户名（超长 / 含控制字符）应被拒绝")
        s4 = socket.create_connection((HOST, PORT), timeout=5)
        s4.settimeout(3)
        send_obj(s4, {"type": "login", "username": "n" * 100})   # 超长
        r4 = recv_obj(s4)
        assert_(r4 is not None and r4.get("type") == "login_fail",
                "超长用户名应被拒绝")
        s4.close()

        s5 = socket.create_connection((HOST, PORT), timeout=5)
        s5.settimeout(3)
        send_obj(s5, {"type": "login", "username": "a\x1fb"})    # 分隔符注入
        r5 = recv_obj(s5)
        assert_(r5 is not None and r5.get("type") == "login_fail",
                "含控制字符（会话键分隔符）的用户名应被拒绝")
        s5.close()

        print("[5] 越权操作应被忽略")
        intruder = connect("fuzz2")
        send_obj(intruder, {"type": "group_msg", "group": "no_such_group",
                            "text": "spam"})     # 不存在的群
        send_obj(intruder, {"type": "group_msg", "group": "secret_group",
                            "text": "spam"})     # 未加入的群
        send_obj(intruder, {"type": "chat", "to": "", "text": "x"})  # 空目标
        send_obj(intruder, {"type": "chat", "to": "fuzz2", "text": "self"})
        send_obj(intruder, {"type": "read", "to": None, "id": None})
        msgs = drain(intruder, 0.5)
        assert_(not any(m.get("type") in ("private", "group") for m in msgs),
                "越权/非法消息未产生任何回显（被服务器正确忽略）")
        intruder.close()

        print("[5.5] 超长单条消息应被拒绝且不落库")
        long_msg = "长" * (client_max + 10)
        buzz = connect("fuzz5")
        buddy = connect("fuzz6")
        drain(buzz, 0.3)  # 清掉登录推送
        send_obj(buzz, {"type": "chat", "to": "fuzz6", "text": long_msg})
        r = drain(buzz, 0.6)
        assert_(any(m.get("type") == "system" and "过长" in m.get("text", "")
                    for m in r), "超长消息应收到“消息过长”系统提示")
        assert_(not any(m.get("type") == "private" for m in r),
                "超长消息不得被回显为已发送")
        got = drain(buddy, 0.4)
        assert_(not any(m.get("type") == "private" for m in got),
                "超长消息不应投递给接收方")
        buzz.close()
        buddy.close()

        print("[5.6] 已登录连接再次 login 不应登记第二个身份（防幽灵用户）")
        ghost = connect("fuzz7")
        drain(ghost, 0.3)
        send_obj(ghost, {"type": "login", "username": "fuzz8"})  # 冒充第二个身份
        watcher = connect("fuzz9")
        seen = drain(watcher, 0.8)
        users = [u for m in seen if m.get("type") == "user_list"
                 for u in m.get("users", [])]
        assert_("fuzz8" not in users, "同一连接不得登记第二个用户名")
        assert_("fuzz7" in users, "原身份应保持在线")
        ghost.close()
        watcher.close()

        print("[5.7] 撤回他人消息应被拒绝（越权）")
        r_a = connect("fuzzA")
        r_b = connect("fuzzB")
        drain(r_a, 0.3)
        drain(r_b, 0.3)
        send_obj(r_b, {"type": "chat", "to": "fuzzA", "text": "bob的话"})
        got_a = drain(r_a, 0.6)
        mid = next(m["id"] for m in got_a if m.get("type") == "private")
        send_obj(r_a, {"type": "recall", "id": mid})  # fuzzA 试图撤回 fuzzB 的消息
        assert_(not any(m.get("type") == "recalled" for m in drain(r_a, 0.5)),
                "越权撤回不应收到撤回确认")
        assert_(not any(m.get("type") == "recalled" for m in drain(r_b, 0.5)),
                "越权撤回不应通知消息作者")
        r_a.close()
        r_a2 = connect("fuzzA")  # 重新登录看历史原文是否被篡改
        replay = drain(r_a2, 0.8)
        assert_(any(m.get("type") == "private" and m.get("text") == "bob的话"
                    for m in replay), "被拒绝的撤回不应改动历史原文")
        # 撤回不存在的消息 id 不应有副作用
        send_obj(r_a2, {"type": "recall", "id": 999999})
        send_obj(r_a2, {"type": "list_users"})
        alive = drain(r_a2, 0.6)
        assert_(any(m.get("type") == "user_list" for m in alive),
                "撤回不存在的消息后服务器仍应正常响应")
        r_a2.close()
        r_b.close()

        print("[5.8] 用户与群组同名应被阻止（避免客户端会话串台）")
        n_owner = connect("fuzzC")
        drain(n_owner, 0.4)
        n_other = connect("fuzzD")
        drain(n_other, 0.4)
        # 群名与在线用户重名 → 拒绝建群
        send_obj(n_other, {"type": "group_create", "group": "fuzzC",
                           "members": []})
        r_n1 = drain(n_other, 0.6)
        assert_(any(m.get("type") == "system" and "占用" in m.get("text", "")
                    for m in r_n1), "群名与在线用户重名应被拒绝")
        assert_(not any(m.get("type") == "group_info" for m in r_n1),
                "被拒绝的建群不应产生群")
        # 离线但登录过的用户名同样受保护：否则对方回来会被“永久锁在门外”
        n_owner.close()
        time.sleep(0.4)
        send_obj(n_other, {"type": "group_create", "group": "fuzzC",
                           "members": []})
        r_n1b = drain(n_other, 0.6)
        assert_(any(m.get("type") == "system" and "占用" in m.get("text", "")
                    for m in r_n1b), "群名不得占用离线用户的名字")
        s_back = connect("fuzzC")   # 原主人仍能正常登录
        r_back = drain(s_back, 0.6)
        assert_(any(m.get("type") == "user_list" for m in r_back),
                "离线用户回来后仍能登录")
        s_back.close()
        # 群名已存在 → 该名字不能再作为用户名登录
        send_obj(n_other, {"type": "group_create", "group": "fuzzgroup",
                           "members": []})
        drain(n_other, 0.5)
        s_clash = socket.create_connection((HOST, PORT), timeout=5)
        s_clash.settimeout(3)
        send_obj(s_clash, {"type": "login", "username": "fuzzgroup"})
        r_n2 = recv_obj(s_clash)
        assert_(r_n2 is not None and r_n2.get("type") == "login_fail",
                "与既有群组同名的用户名应被拒绝")
        s_clash.close()
        n_other.close()

        print("[5.85] 同名重登：旧连接必须收到 kicked 通知（否则双方互踢死循环）")
        t1 = connect("fuzzE")
        drain(t1, 0.5)
        t2 = connect("fuzzE")           # 同一账号再次登录
        r_t1 = drain(t1, 1.0)
        assert_(any(m.get("type") == "kicked" for m in r_t1),
                f"被顶掉的旧连接应收到 kicked: {r_t1}")
        r_t2 = drain(t2, 0.6)
        assert_(any(m.get("type") == "user_list" for m in r_t2),
                "接管的新连接应正常工作")
        # 旧连接被关闭后，服务器不应把它误判为“新会话掉线”
        send_obj(t2, {"type": "list_users"})
        assert_(any(m.get("type") == "user_list" for m in drain(t2, 0.6)),
                "接管连接在旧连接关闭后仍应存活")
        t1.close()
        t2.close()

        print("[5.87] 越权防护：非群成员不得自封入群 / 替别人拉人")
        sec_a = connect("sectA")
        drain(sec_a, 0.4)
        sec_b = connect("sectB")
        drain(sec_b, 0.4)
        send_obj(sec_a, {"type": "group_create", "group": "封密群",
                         "members": []})
        drain(sec_a, 0.5)
        # 局外人用同名 group_create 试图把自己塞进去
        send_obj(sec_b, {"type": "group_create", "group": "封密群",
                         "members": ["sectB"]})
        r_sec = drain(sec_b, 0.6)
        assert_(any(m.get("type") == "system" and "不是群聊" in m.get("text", "")
                    for m in r_sec),
                f"非成员不得自封入群（应收到拒绝提示）: {r_sec}")
        send_obj(sec_b, {"type": "group_members", "group": "封密群"})
        r_sec2 = drain(sec_b, 0.6)
        assert_(not any(m.get("type") == "group_info" for m in r_sec2),
                "被拒绝后不应拿到群成员名单")
        # 群成员邀请：正常生效
        send_obj(sec_a, {"type": "group_create", "group": "封密群",
                         "members": ["sectB"]})
        r_ok = drain(sec_b, 0.6)
        assert_(any(m.get("type") == "group_info"
                    and "sectB" in m.get("members", []) for m in r_ok),
                f"成员邀请应生效: {r_ok}")
        # 非法的成员名（含控制字符）/ 与群名同名的成员名：必须回告而不是静默丢弃
        send_obj(sec_a, {"type": "group_create", "group": "封密群",
                         "members": ["坏\n名字", "封密群"]})
        r_bad = drain(sec_a, 0.6)
        assert_(any(m.get("type") == "system" and "已忽略成员" in m.get("text", "")
                    for m in r_bad), f"非法/冲突成员名应回告: {r_bad}")
        sec_a.close()
        sec_b.close()

        print("[5.88] 心跳 ping/pong 与单连接限流")
        hb = connect("hbUser")
        drain(hb, 0.5)
        send_obj(hb, {"type": "ping", "ts": 12345})
        r_pong = drain(hb, 0.6)
        assert_(any(m.get("type") == "pong" and m.get("ts") == 12345
                    for m in r_pong), f"ping 应得到 pong: {r_pong}")
        # 心跳不应被当成业务消息污染会话，也不影响后续正常通信
        send_obj(hb, {"type": "list_users"})
        assert_(any(m.get("type") == "user_list" for m in drain(hb, 0.6)),
                "心跳之后仍能正常通信")
        # 疯狂刷屏：超限时服务器应提示且不崩、不断开
        for _ in range(RATE_LIMIT + 60):
            try:
                send_obj(hb, {"type": "list_users"})
            except OSError:
                break
        r_flood = drain(hb, 1.2)
        assert_(any(m.get("type") == "system"
                    and "频繁" in m.get("text", "") for m in r_flood),
                "刷屏应被限流并提示")
        send_obj(hb, {"type": "ping", "ts": 1})   # 限流不误伤心跳
        assert_(any(m.get("type") == "pong" for m in drain(hb, 0.6)),
                "限流不应影响心跳")
        hb.close()

        print("[5.9] 存储文件损坏时，服务器应能启动（备份坏文件）")
        bad_path = os.path.join(tempfile.gettempdir(), "moyu_bad_store.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("{ 这不是合法 JSON")
        env_bad = dict(os.environ, MOYU_PORT=str(PORT + 1), MOYU_STORE=bad_path)
        proc_bad = subprocess.Popen([sys.executable, "server.py"], env=env_bad,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.STDOUT)
        try:
            time.sleep(1.5)
            assert_(proc_bad.poll() is None,
                    "存储文件损坏不应导致服务器启动失败")
            b1 = socket.create_connection((HOST, PORT + 1), timeout=5)
            b1.settimeout(3)
            send_obj(b1, {"type": "login", "username": "bad1"})
            r_bad = recv_obj(b1)
            assert_(r_bad is not None and r_bad.get("type") == "login_ok",
                    "坏存储下仍可正常登录")
            b1.close()
            assert_(os.path.exists(bad_path + ".corrupt"),
                    "损坏的存储文件应被备份为 .corrupt")
        finally:
            proc_bad.terminate()
            proc_bad.wait()
            for p in (bad_path, bad_path + ".corrupt"):
                try:
                    os.remove(p)
                except Exception:
                    pass

        print("[6] 上述异常之后，服务器仍能正常服务新用户")
        late = connect("fuzz3")
        peer = connect("fuzz4")
        send_obj(late, {"type": "chat", "to": "fuzz4", "text": "still alive"})
        got = drain(peer, 0.8)
        assert_(any(m.get("type") == "private" and m["text"] == "still alive"
                    for m in got), "服务器仍能正常路由私聊（未崩溃）")
        late.close()
        peer.close()

        print(f"\n✅ 健壮性测试全部通过（{PASS} 项断言）")
    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
