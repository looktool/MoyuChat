"""全功能模拟测试：模拟多用户跑通所有功能，并验证服务端重启后数据持久化。

使用独立端口(9100)与临时存储文件，不干扰默认 9000 端口上你正在运行的服务器。
测试覆盖：
  私聊 / 群聊(建群+邀请) / 单双勾回执 / 离线私聊 / 离线群消息(重连补投) /
  typing 转发 / presence 上下线 / 邀请离线用户 / 退出群组 /
  服务端重启后群历史与私聊历史持久化（回放消息带 replay 标记，不计未读）。
"""

import os
import socket
import subprocess
import sys
import tempfile
import time

# 必须在导入 common/server 之前设置环境变量（模块在导入时读取端口）
os.environ["MOYU_PORT"] = "9100"   # 主聊天端口（同一时刻只跑一个实例，故固定）
os.environ["MOYU_STORE"] = os.path.join(
    tempfile.gettempdir(), "moyu_test_store.json")
# 清理旧存储，保证“全新”状态
try:
    os.remove(os.environ["MOYU_STORE"])
except FileNotFoundError:
    pass

from common import send_obj, recv_obj, HOST, PORT  # noqa: E402


def launch(idx=0):
    """启动一个服务器子进程；联邦/发现端口按实例下标偏移，避免同机多实例互撞。"""
    env = dict(os.environ)
    env["MOYU_FED_PORT"] = str(9101 + idx)        # 联邦监听端口
    env["MOYU_DISCOVERY_PORT"] = str(9201 + idx)  # UDP 自动发现端口
    return subprocess.Popen([sys.executable, "server.py"], env=env)


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
    s.settimeout(0.25)
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


def assert_(cond, msg):
    if not cond:
        raise AssertionError("❌ " + msg)
    print("  ✓", msg)


def main():
    proc = launch(0)
    time.sleep(1.5)
    try:
        print("[1] 登录与基础连接")
        a = connect("alice")
        b = connect("bob")
        c = connect("carol")

        m = collect(b)
        assert_("alice" in [u for x in m if x.get("type") == "user_list"
                            for u in x["users"]], "bob 收到含 alice 的在线列表")

        print("[2] 建群 + 邀请成员")
        send_obj(a, {"type": "group_create", "group": "team",
                     "members": ["bob", "carol"]})
        mb = collect(b, 0.5)
        mc = collect(c, 0.5)
        assert_(any(x.get("type") == "group_info" and x["group"] == "team"
                    and set(["alice", "bob", "carol"]).issubset(set(x["members"]))
                    for x in mb), "bob 收到 team 群成员信息(含三人)")
        assert_(any(x.get("type") == "group_info" and x["group"] == "team"
                    for x in mc), "carol 收到 team 群成员信息")

        print("[3] 群消息广播")
        send_obj(a, {"type": "group_msg", "group": "team", "text": "hello team"})
        mb = collect(b, 0.4)
        mc = collect(c, 0.4)
        assert_(any(x.get("type") == "group" and x["group"] == "team"
                    and x["text"] == "hello team" for x in mb), "bob 收到群消息")
        assert_(any(x.get("type") == "group" and x["group"] == "team"
                    and x["text"] == "hello team" for x in mc), "carol 收到群消息")

        print("[4] 私聊 + 单双勾回执")
        send_obj(a, {"type": "chat", "to": "bob", "text": "hi bob"})
        mb = collect(b, 0.4)
        priv = next((x for x in mb if x.get("type") == "private"
                     and x["from"] == "alice"), None)
        assert_(priv is not None, "bob 收到私聊")
        send_obj(b, {"type": "read", "target": "alice", "id": priv["id"],
                     "to": "alice"})
        ma = collect(a, 0.4)
        rc = next((x for x in ma if x.get("type") == "read_receipt"
                   and x["id"] == priv["id"]), None)
        assert_(rc is not None, "alice 收到已读回执(双勾)")
        assert_(rc["target"] == "bob",
                f"回执会话名须换算成 alice 视角的会话名: {rc}")
        # 越权伪造回执：carol 并非该私聊接收方，不应让 alice 的消息变成已读
        send_obj(c, {"type": "read", "target": "alice", "id": priv["id"],
                     "to": "alice"})
        ma2 = collect(a, 0.4)
        assert_(not any(x.get("type") == "read_receipt"
                        and x["id"] == priv["id"] for x in ma2),
                "非接收方伪造的已读回执应被拒绝")
        # 批量回执：一次回执多条，服务端逐条校验后分别转发
        mid_list = []
        for i in range(3):
            send_obj(a, {"type": "chat", "to": "bob", "text": f"批量-{i}"})
            got = collect(b, 0.4)
            mid_list.append(next(x["id"] for x in got
                                 if x.get("type") == "private"
                                 and x.get("text") == f"批量-{i}"))
        send_obj(b, {"type": "read", "target": "alice", "ids": mid_list
                     + [987650, "脏数据"]})
        mb3 = collect(a, 0.8)
        got_ids = [x["id"] for x in mb3 if x.get("type") == "read_receipt"]
        assert_(set(mid_list) <= set(got_ids),
                f"批量回执应逐条转发: {got_ids} vs {mid_list}")
        assert_(all(x["target"] == "bob" for x in mb3
                    if x.get("type") == "read_receipt"),
                "批量回执的会话名也应换算成接收方视角")
        assert_(987650 not in got_ids, "批量回执中的无效 id 应被忽略")
        # 回执指向不存在的消息 id 时不应崩溃
        send_obj(b, {"type": "read", "target": "alice", "id": 987654,
                     "to": "alice"})
        send_obj(b, {"type": "list_groups"})
        mb2 = collect(b, 0.5)
        assert_(any(x.get("type") == "group_list" for x in mb2),
                "异常回执后服务器仍应正常响应")

        print("[5] 离线私聊：dave 离线时 alice 发给 dave，dave 上线后收到")
        d = None  # dave 尚未登录
        send_obj(a, {"type": "chat", "to": "dave", "text": "离线留言"})
        collect(a, 0.3)  # alice 收到回显(单勾)
        d = connect("dave")
        md = collect(d, 0.6)
        assert_(any(x.get("type") == "private" and x["from"] == "alice"
                    and x["text"] == "离线留言" for x in md), "dave 上线收到离线私聊")

        print("[6] 离线群消息：carol 掉线后 bob 发群消息，carol 重连补投")
        c.close()  # carol 掉线
        time.sleep(0.3)
        send_obj(b, {"type": "group_msg", "group": "team",
                     "text": "after carol left"})
        time.sleep(0.3)
        c = connect("carol")  # 重连
        mc = collect(c, 0.8)
        assert_(any(x.get("type") == "group" and x["group"] == "team"
                    and x["text"] == "after carol left" for x in mc),
                "carol 重连后补收到离线期间的群消息")

        print("[7] presence：carol 掉线后其他人收到离线广播")
        # 重新登录 carol 时，之前掉线应已广播过；这里仅验证当前在线列表完整
        mb = collect(b, 0.3)
        assert_(any(x.get("type") == "user_list" and "carol" in x["users"]
                    for x in mb), "bob 看到 carol 重新在线")

        print("[8] typing 转发")
        send_obj(a, {"type": "typing", "conv": "bob"})
        mb = collect(b, 0.4)
        assert_(any(x.get("type") == "typing" and x["from"] == "alice"
                    for x in mb), "bob 收到 alice 的 typing 事件")

        print("[8.5] 邀请离线用户 + 退出群组")
        send_obj(a, {"type": "group_create", "group": "team",
                     "members": ["erin"]})  # erin 当前离线
        collect(a, 0.3)
        e = connect("erin")
        me = collect(e, 0.8)
        assert_(any(x.get("type") == "group_list" and "team" in x["groups"]
                    for x in me), "erin 上线后看到被加入的 team 群")
        assert_(any(x.get("type") == "group" and x["group"] == "team"
                    for x in me), "erin 上线后收到 team 群历史")
        # carol 退出群组
        send_obj(c, {"type": "group_leave", "group": "team"})
        mc2 = collect(c, 0.5)
        assert_(any(x.get("type") == "left_group" and x["group"] == "team"
                    for x in mc2), "carol 收到退群事件")
        ma2 = collect(a, 0.5)
        assert_(any(x.get("type") == "group_info" and x["group"] == "team"
                    and "carol" not in x["members"] for x in ma2),
                "alice 收到更新后的群成员（已不含 carol）")

        print("[8.6] 发现群聊 + 主动加入群组")
        send_obj(d, {"type": "list_all_groups"})
        md = collect(d, 0.5)
        allg = next((x for x in md if x.get("type") == "all_groups"), None)
        assert_(allg is not None, "应返回 all_groups 列表")
        names = {g["name"]: g for g in allg["groups"]}
        assert_("team" in names, "列表应包含 team 群")
        assert_(names["team"]["count"] >= 2, "team 群人数应正确")
        assert_(names["team"]["joined"] is False, "dave 尚未加入 team")
        send_obj(d, {"type": "group_join", "group": "team"})
        md2 = collect(d, 0.8)
        assert_(any(x.get("type") == "system" and "已加入群组 team"
                    in x.get("text", "") for x in md2), "加入后应收到提示")
        assert_(any(x.get("type") == "group_info" and x["group"] == "team"
                    and "dave" in x["members"] for x in md2),
                "加入后群成员应包含 dave")
        send_obj(d, {"type": "list_all_groups"})
        md3 = collect(d, 0.5)
        allg3 = next(x for x in md3 if x.get("type") == "all_groups")
        joined = {g["name"]: g["joined"] for g in allg3["groups"]}
        assert_(joined.get("team") is True, "加入后 joined 应为 True")
        # 退群后再发言：不能静默丢弃，必须明确告知发送失败
        send_obj(d, {"type": "group_leave", "group": "team"})
        collect(d, 0.5)
        send_obj(d, {"type": "group_msg", "group": "team", "text": "我还在吗"})
        md4 = collect(d, 0.6)
        assert_(any(x.get("type") == "system" and "不在群聊" in x.get("text", "")
                    for x in md4), f"退群后发言应收到失败提示: {md4}")
        assert_(not any(x.get("type") == "group" and x.get("text") == "我还在吗"
                        for x in md4), "退群后的消息不应被群发")
        send_obj(b, {"type": "list_groups"})   # 群成员不应看到这条消息
        assert_(not any(x.get("type") == "group" and x.get("text") == "我还在吗"
                        for x in collect(b, 0.5)),
                "其他人的客户端不应收到退群者的消息")

        print("[8.7] 群成员按需查询（仅群内成员可见）")
        send_obj(b, {"type": "group_members", "group": "team"})
        mb7 = collect(b, 0.5)
        gi = next((x for x in mb7 if x.get("type") == "group_info"
                   and x.get("group") == "team"), None)
        assert_(gi is not None, f"群成员应能查到成员列表: {mb7}")
        assert_({"alice", "bob"} <= set(gi["members"]),
                f"成员列表不完整: {gi}")
        # 非成员查询（dave 已退群）：不应泄露成员名单
        send_obj(d, {"type": "group_members", "group": "team"})
        md5 = collect(d, 0.5)
        assert_(not any(x.get("type") == "group_info" and x.get("group") == "team"
                        for x in md5), "非群成员不应查到成员名单")
        assert_(any(x.get("type") == "system" and "不在群聊" in x.get("text", "")
                    for x in md5), f"非成员查询应收到提示: {md5}")

        print("[8.8] 消息撤回（私聊 + 群聊）")
        send_obj(a, {"type": "chat", "to": "bob", "text": "这条要撤回"})
        ra = collect(a, 0.4)
        mid = next(x["id"] for x in ra
                   if x.get("type") == "private" and x["text"] == "这条要撤回")
        rb = collect(b, 0.4)
        assert_(any(x.get("type") == "private" and x.get("id") == mid
                    for x in rb), "bob 先收到待撤回消息")
        send_obj(a, {"type": "recall", "id": mid})
        ra2 = collect(a, 0.5)
        rb2 = collect(b, 0.5)
        ra2_rc = next((x for x in ra2 if x.get("type") == "recalled"
                       and x["id"] == mid), None)
        rb2_rc = next((x for x in rb2 if x.get("type") == "recalled"
                       and x["id"] == mid), None)
        assert_(ra2_rc is not None, "发送方收到撤回确认")
        assert_(rb2_rc is not None, "接收方收到撤回通知")
        # 双方对同一私聊的会话名不同：各自必须收到自己视角的名字，否则界面不刷新
        assert_(ra2_rc["conv"] == "bob", f"发送方视角会话名应为 bob: {ra2_rc}")
        assert_(rb2_rc["conv"] == "alice",
                f"接收方视角会话名应为 alice: {rb2_rc}")
        # 群聊撤回
        send_obj(a, {"type": "group_msg", "group": "team",
                     "text": "群里的这条也要撤回"})
        ga = collect(a, 0.4)
        gmid = next(x["id"] for x in ga
                    if x.get("type") == "group"
                    and x["text"] == "群里的这条也要撤回")
        collect(b, 0.4)  # 排空 bob 的副本
        collect(e, 0.4)
        send_obj(a, {"type": "recall", "id": gmid})
        gb = collect(b, 0.5)
        assert_(any(x.get("type") == "recalled" and x["id"] == gmid
                    for x in gb), "群成员收到群消息撤回通知")

        print("[9] 持久化：重启服务器后群与历史仍在")
        for s in (a, b, c, d, e):
            s.close()
        proc.terminate()
        proc.wait()
        time.sleep(0.5)
        proc2 = launch(1)
        time.sleep(1.5)
        try:
            a2 = connect("alice")
            mg = collect(a2, 0.8)
            assert_(any(x.get("type") == "group_list" and "team" in x["groups"]
                        for x in mg), "重启后 team 群仍然存在")
            assert_(any(x.get("type") == "group" and x["group"] == "team"
                        and x["text"] == "hello team" for x in mg),
                    "重启后 alice 登录补收到群历史")
            assert_(any(x.get("type") == "group" and x.get("replay") is True
                        for x in mg), "补投的群历史带 replay 标记（不计未读）")
            assert_(any(x.get("type") == "private" and x["text"] == "hi bob"
                        for x in mg), "重启后仍能恢复私聊历史(alice↔bob)")
            assert_(any(x.get("type") == "private" and x["text"] == "离线留言"
                        for x in mg), "重启后仍能恢复私聊历史(alice↔dave)")
            assert_(any(x.get("type") == "private" and x.get("replay") is True
                        for x in mg), "补投的私聊历史带 replay 标记")
            assert_(not any(x.get("type") == "private"
                            and x.get("text") == "这条要撤回" for x in mg),
                    "重启后被撤回的私聊消息不再出现原文")
            assert_(any(x.get("type") == "private" and x.get("recalled")
                        for x in mg), "重启后撤回状态仍保留（墓碑落盘）")
            assert_(not any(x.get("type") == "group"
                            and x.get("text") == "群里的这条也要撤回"
                            for x in mg),
                    "重启后被撤回的群消息不再出现原文")
            # bob 重启后应能看到与 alice 的私聊历史
            b2 = connect("bob")
            mb2 = collect(b2, 0.8)
            assert_(any(x.get("type") == "private" and x["text"] == "hi bob"
                        for x in mb2), "bob 重启后也能恢复与 alice 的私聊历史")
            # 重启后新消息 ID 必须全局唯一（客户端按 id 去重，撞车会丢消息）
            send_obj(a2, {"type": "chat", "to": "bob", "text": "重启后的新消息"})
            ma_new = collect(a2, 0.5)
            new_msg = next((x for x in ma_new if x.get("type") == "private"
                            and x["text"] == "重启后的新消息"), None)
            assert_(new_msg is not None, "重启后仍可正常私聊")
            all_ids = [x["id"] for x in mg if "id" in x] + [new_msg["id"]]
            assert_(len(set(all_ids)) == len(all_ids),
                    "消息 ID 应全局唯一（无撞车，避免客户端去重丢消息）")
            b2.close()
            print("  ✓ 持久化验证通过（群历史 + 私聊历史）")
        finally:
            proc2.terminate()
            proc2.wait()

        print("[10] 同名重登接管旧连接（断线自动重连的基础）")
        proc3 = launch(2)
        time.sleep(1.5)
        try:
            f1 = connect("frank")          # 旧会话
            f2 = connect("frank")          # 同名再次登录，应接管旧连接
            # 新连接仍可正常通信
            send_obj(f2, {"type": "list_users"})
            mu = collect(f2, 0.5)
            assert_(any(x.get("type") == "user_list" for x in mu),
                    "同名重登后新连接可正常通信")
            # 旧连接应被服务器关闭（先排空残留缓冲，再等待 EOF）
            f1.settimeout(0.5)
            closed = False
            deadline = time.time() + 3.0
            while time.time() < deadline:
                try:
                    data = f1.recv(4096)
                except socket.timeout:
                    continue
                except Exception:
                    closed = True
                    break
                if data == b"":  # 对端关闭
                    closed = True
                    break
            assert_(closed, "旧连接已被服务器关闭（由新连接接管）")
            f2.close()
            f1.close()
        finally:
            proc3.terminate()
            proc3.wait()

        print("\n✅ 全功能模拟测试通过：私聊/群聊/单双勾/离线私聊/离线群消息/"
              "typing/presence/持久化 均正常")
    finally:
        try:
            proc.terminate()
            proc.wait()
        except Exception:
            pass


if __name__ == "__main__":
    main()
