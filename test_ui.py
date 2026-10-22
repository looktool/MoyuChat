"""UI 交互测试（无头）：验证点击会话行能选中、能发送消息、悬停/右键绑定不冲突。

回归背景：bind_tree 曾写死绑定 <Button-1>，导致悬停绑定覆盖了点击选中，
表现为“点了头像/会话没反应、发不出消息”。
"""

import json
import struct
import tkinter as tk
import time

import client


class FakeSock:
    """记录 send_obj 发出的报文（不发真实网络）。"""

    def __init__(self):
        self.buf = b""
        self.sent = []

    def sendall(self, data):
        self.buf += data
        while len(self.buf) >= 4:
            n = struct.unpack(">I", self.buf[:4])[0]
            if len(self.buf) < 4 + n:
                break
            payload = self.buf[4:4 + n]
            self.buf = self.buf[4 + n:]
            self.sent.append(json.loads(payload.decode("utf-8")))


def menu_labels(menu):
    """取出菜单里的命令项文本（分隔符等无 label 的项会被跳过）。"""
    out = []
    for i in range(menu.index("end") + 1):
        if menu.type(i) == "command":
            out.append(menu.entrycget(i, "label"))
    return out


def main():
    root = tk.Tk()
    c = client.ChatClient(root)
    c.username = "me"
    c.sock = FakeSock()
    c.build_main()
    c.presence["bob"] = True
    c.ensure_conv("bob", "user")
    c.ensure_conv("dev", "group")
    c.group_members["dev"] = ["me", "bob"]
    c.render_sidebar()
    root.deiconify()
    root.update()

    # 1) 点击会话行应能选中（核心回归点）
    rows = c.conv_inner.winfo_children()
    assert rows, "会话列表为空"
    rows[0].event_generate("<Button-1>", x=5, y=5)
    root.update()
    assert c.active == "bob", f"点击会话行未选中，active={c.active}"
    print("✅ 点击会话行可正确选中:", c.active)

    # 2) 选中后输入并发送
    c.on_input_focus_in(None)          # 清除占位
    c.input.delete("1.0", tk.END)
    c.input.insert("1.0", "hello")
    c.send_msg()
    root.update()
    sent = c.sock.sent
    assert any(m.get("type") == "chat" and m.get("to") == "bob"
               and m.get("text") == "hello" for m in sent), \
        f"未发出私聊消息: {sent}"
    assert c.input.get("1.0", tk.END).strip() in ("", "消息"), "发送后输入框未清空"
    print("✅ 选中后输入并发送正常")

    # 2.5) 发送失败（断线）时应保留草稿，避免用户输入丢失
    class BadSock:
        def sendall(self, data):
            raise OSError("broken pipe")

    good_sock, good_reconnect = c.sock, c.start_reconnect
    c.sock, c.start_reconnect = BadSock(), (lambda: None)
    c.select_conv("bob")
    c.input.delete("1.0", tk.END)
    c.input.insert("1.0", "draft-kept")
    c.send_msg()
    assert c.input.get("1.0", tk.END).strip() == "draft-kept", \
        f"发送失败后草稿被清空: {c.input.get('1.0', tk.END)!r}"
    assert c.sock is None, "发送失败应进入断线状态"
    c.sock, c.start_reconnect = good_sock, good_reconnect
    c.input.delete("1.0", tk.END)
    print("✅ 断线发送失败保留草稿")

    # 3) 右键菜单构建正确（直接构建，避免 popup 阻塞）
    menu = c.build_conv_menu("bob")
    labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
              if menu.type(i) != "separator"]
    assert "删除会话" in labels, f"菜单缺少删除项: {labels}"
    gmenu = c.build_conv_menu("dev")
    glabels = [gmenu.entrycget(i, "label")
               for i in range(gmenu.index("end") + 1)
               if gmenu.type(i) != "separator"]
    assert "退出群组" in glabels, f"群菜单缺少退群项: {glabels}"
    print("✅ 右键菜单构建正常:", labels)

    # 4) 置顶/删除会话逻辑
    c.toggle_pin("bob")
    assert "bob" in c.pinned
    c.pinned.discard("bob")
    print("✅ 置顶开关正常")

    # 4.5) 搜索：会话名与消息内容均可命中
    c.store_message("dev", "bob", "排期确认一下", time.time(), "group", mid=7)
    assert c.conv_matches("dev", "排期"), "搜索应能命中历史消息内容"
    assert c.conv_matches("dev", "de"), "搜索应能命中会话名"
    assert not c.conv_matches("bob", "zzz-不存在"), "无关会话不应命中"
    c.search_term = "排期"
    c.render_sidebar()
    assert any(isinstance(w, tk.Frame)
               for w in c.conv_inner.winfo_children()), "按内容搜索后列表不应为空"
    c.search_term = ""
    c.render_sidebar()
    print("✅ 搜索命中会话名与消息内容")

    # 4.6) 气泡右键：复制 / 删除消息（本地删除）
    c.select_conv("bob")
    c.store_message("bob", "bob", "copy-me", time.time(), "user", mid=8)
    entry = c.buffers["bob"][-1]
    bmenu = c.build_bubble_menu(entry)
    blabels = menu_labels(bmenu)
    assert "复制" in blabels and "删除消息" in blabels, blabels
    c.copy_message(entry)
    assert root.clipboard_get() == "copy-me", "复制未写入剪贴板"
    n_before = len(c.buffers["bob"])
    c.delete_message(entry)
    assert len(c.buffers["bob"]) == n_before - 1, "删除消息未生效"
    assert all(e is not entry for e in c.buffers["bob"]), "目标消息仍存在"
    print("✅ 气泡右键：复制 / 删除消息")

    # 4.7) 撤回：只能撤回自己未超时的消息，收到广播后本地置为占位
    c.select_conv("bob")
    c.store_message("bob", "bob", "对方的消息", time.time(), "user", mid=21)
    other = c.buffers["bob"][-1]
    assert c.can_recall(other) is False, "不能撤回他人消息"
    c.store_message("bob", c.username, "我的消息", time.time(), "user",
                    mid=22, outgoing=True)
    mine = c.buffers["bob"][-1]
    assert c.can_recall(mine) is True, "自己的近期消息应可撤回"
    mm = c.build_bubble_menu(mine)
    mlabels = menu_labels(mm)
    assert "撤回" in mlabels, mlabels
    stale = dict(mine, ts=time.time() - 3600)
    assert c.can_recall(stale) is False, "超出时间窗的消息不应可撤回"
    c.on_recalled(22, "bob")
    assert mine.get("recalled") is True and mine["text"] == "", \
        "收到撤回广播后应清空原文"
    assert c.preview_of("bob") == "消息已撤回", c.preview_of("bob")
    # 私聊双方对会话的叫法不同：对方视角的会话名也要能命中（按 id 兜底）
    c.store_message("bob", c.username, "对方视角撤回", time.time(), "user",
                    mid=23, outgoing=True)
    other_view = c.buffers["bob"][-1]
    c.on_recalled(23, "tanuki")   # tanuki 是“对方”那边的会话名
    assert other_view.get("recalled") is True, "会话名不一致时也应命中"
    c.store_message("bob", "bob", "对方发来的消息", time.time(), "user",
                    mid=24)
    other_person = c.buffers["bob"][-1]
    # 广播声称是我发的，实际这条是对方发的 → 不应被标记（防伪造）
    c.on_recalled(24, "bob", c.username)
    assert other_person.get("recalled") is not True, "不应篡改他人消息的归属"
    c.on_recalled(24, "bob", "bob")
    assert other_person.get("recalled") is True, "作者匹配时应正常撤回"
    c.on_recalled("脏数据", "bob")   # 脏报文不应抛异常
    rlabels = menu_labels(c.build_bubble_menu(mine))
    assert "复制" not in rlabels, f"已撤回消息不应提供复制: {rlabels}"
    print("✅ 消息撤回（限本人/限时/广播后置为占位）")

    # 4.8) 发现群聊：列表填充、加入、窗口关闭
    c.open_discover()
    assert c.discover_win is not None and c.discover_win.winfo_exists(), \
        "发现群聊窗口未创建"
    c.sock.sent.clear()
    c.on_all_groups([{"name": "team", "count": 3, "joined": False},
                     {"name": "design", "count": 5, "joined": True}])
    assert c.discover_list.size() == 2, c.discover_list.size()
    assert "已加入" in c.discover_list.get(1), c.discover_list.get(1)
    c.discover_list.selection_set(0)
    c.join_selected_group()
    joins = [o for o in c.sock.sent if o.get("type") == "group_join"]
    assert joins and joins[-1]["group"] == "team", joins
    assert c.discover_win is None, "加入后应关闭发现窗口"
    c.on_all_groups([])  # 窗口已关闭：只更新缓存，不应报错
    assert c.discover_groups == []
    c.handle_msg({"type": "all_groups",
                  "groups": [{"name": "x", "count": 1, "joined": False},
                             "脏数据", None]})
    assert [g["name"] for g in c.discover_groups] == ["x"], c.discover_groups
    print("✅ 发现群聊：列表填充 / 一键加入 / 脏数据过滤")

    # 5) 群聊下可发出群消息
    c.select_conv("dev")
    c.input.delete("1.0", tk.END)
    c.input.insert("1.0", "hi team")
    c.send_msg()
    root.update()
    assert any(m.get("type") == "group_msg" and m.get("group") == "dev"
               and m.get("text") == "hi team" for m in c.sock.sent), \
        "未发出群消息"
    print("✅ 群聊发送正常")

    # 6) 打开会话即回执已读（否则对方一直是单勾）
    c.select_conv("dev")                 # 切到群聊，让 bob 的会话变为非激活
    c.sock.sent.clear()
    c.store_message("bob", "bob", "ping", time.time(), "user", mid=100)
    assert c.unread.get("bob") == 1, f"非激活会话应累计未读: {c.unread}"
    c.select_conv("bob")                 # 打开会话应发出已读回执
    assert any(m.get("type") == "read" and m.get("id") == 100
               for m in c.sock.sent), f"打开会话未回执已读: {c.sock.sent}"
    print("✅ 打开会话即回执已读（双勾）")

    # 7) 回放历史不计未读
    c.select_conv("dev")
    before = c.unread.get("dev", 0)
    c.store_message("dev", "bob", "old", time.time(), "group", mid=101,
                    replay=True)
    assert c.unread.get("dev", 0) == before, "回放历史不应计入未读"
    print("✅ 回放历史不计未读")

    # 8) “正在输入…”同时体现在头部与列表预览
    c.select_conv("bob")
    c.on_typing("bob", "bob")
    assert c.typing_conv == "bob", "typing_conv 未设置"
    assert c.preview_of("bob") == "正在输入…", "列表预览未显示输入中"
    assert "正在输入" in c.hs["text"], f"头部未显示输入中: {c.hs['text']}"
    c.clear_typing()
    assert c.typing_conv is None, "clear_typing 未清除状态"
    # 群聊里要能看出是谁在输入（头部与列表预览都要带昵称）
    c.select_conv("dev")
    c.on_typing("bob", "dev")
    assert c.preview_of("dev") == "bob 正在输入…", c.preview_of("dev")
    assert c.hs["text"] == "bob 正在输入…", c.hs["text"]
    c.update_header()  # 头部被其他事件刷新后仍应保留输入者昵称
    assert c.hs["text"] == "bob 正在输入…", f"刷新后丢失输入者: {c.hs['text']}"
    c.clear_typing()
    assert c.typing_from is None, "clear_typing 未清除输入者"
    print("✅ 正在输入提示（私聊/群聊头部 + 列表预览）")

    # 9) 未读数上限显示 99+
    c.unread["bob"] = 250
    c.render_sidebar()
    assert c.unread["bob"] == 250  # 只是显示上限，数据不变
    print("✅ 未读数上限（99+）")

    # 9.5) 气泡换行宽度随窗口自适应（收窄应重排，微小变化不重排）
    c.select_conv("bob")
    c.wrap_px = 420
    c.on_chat_resize(type("E", (), {"width": 300})())   # 视口收窄
    assert c.wrap_px < 420, f"窗口收窄后换行宽度未变小: {c.wrap_px}"
    assert c.resize_job is not None, "应变体重排（防抖任务未创建）"
    if c.resize_job is not None:
        root.after_cancel(c.resize_job)
        c.resize_job = None
    kept = c.wrap_px
    c.on_chat_resize(type("E", (), {"width": 305})())   # 变化不大：不重排
    assert c.resize_job is None and c.wrap_px == kept, "微小尺寸变化不应触发重排"
    c.on_chat_resize(type("E", (), {"width": 1200})())  # 超宽：换行宽度有上限
    assert c.wrap_px <= 560, f"换行宽度应有上限: {c.wrap_px}"
    if c.resize_job is not None:
        root.after_cancel(c.resize_job)
        c.resize_job = None
    c.render_chat()
    print("✅ 气泡换行宽度随窗口自适应")

    # 9.5) 已读回执：打开会话补发未回执消息；对端回执按 id 兜底匹配
    c.ensure_conv("erin", "user")  # 非当前打开会话，便于观察回执发送时机
    c.store_message("erin", "erin", "未回执的来消息", time.time(), "user",
                    mid=31)
    c.store_message("erin", c.username, "我发的", time.time(), "user",
                    mid=32, outgoing=True)
    incoming = [e for e in c.buffers["erin"] if e.get("id") == 31][0]
    mine = [e for e in c.buffers["erin"] if e.get("id") == 32][0]
    assert incoming["receipt_sent"] is False, "非当前会话不应自动回执"
    c.sock.sent.clear()
    c.send_read_receipts("erin")
    reads = [o for o in c.sock.sent if o.get("type") == "read"]
    assert [r["id"] for r in reads] == [31], reads
    assert reads[0]["to"] == "erin" and reads[0]["target"] == "erin", reads[0]
    c.send_read_receipts("erin")   # 已回执过的不应重复发送
    assert [o for o in c.sock.sent if o.get("type") == "read"] == reads, \
        "重复回执不应重复发送"
    assert mine["status"] != "read", "未收到回执前不应是已读"
    # 会话名对不上（对方视角）时也应按 id 找到并置为已读
    c.handle_msg({"type": "read_receipt", "target": "tanuki", "id": 32})
    assert mine["status"] == "read", "回执会话名不一致时应按 id 兜底匹配"
    # 非自己发的消息 / 不存在的 id 不应被误改状态，也不应报错
    c.handle_msg({"type": "read_receipt", "target": "erin", "id": 31})
    assert incoming["status"] != "read", "不应把对方的消息标成已读"
    c.handle_msg({"type": "read_receipt", "target": "erin", "id": 999999})
    c.handle_msg({"type": "read_receipt"})
    # 积压未读时合并成一条批量回执，避免打开会话瞬间涌出几百个报文
    c.buffers["erin"] = []
    c.seen_ids["erin"] = set()
    for i in range(30):
        c.store_message("erin", "erin", f"积压{i}", time.time(), "user",
                        mid=5000 + i)
    c.sock.sent.clear()
    c.send_read_receipts("erin")
    reads2 = [o for o in c.sock.sent if o.get("type") == "read"]
    assert len(reads2) == 1, f"30 条未读应合并为 1 条批量回执: {len(reads2)}"
    assert reads2[0].get("ids") == [5000 + i for i in range(30)], reads2[0]
    before = len(c.sock.sent)
    c.send_read_receipts("erin")     # 再发一次不应重发
    assert len(c.sock.sent) == before, "批量回执后不应重复发送"
    print("✅ 已读回执（补发去重 / 批量合并 / 会话名兜底 / 脏回执防护）")

    # 9.6) 群成员列表弹窗：按需拉取 / 显示在线状态 / 成员变化自动刷新
    c.select_conv("dev")
    c.group_members["dev"] = ["me", "bob"]
    c.presence["bob"] = True
    c.presence["carol"] = False
    c.sock.sent.clear()
    c.show_members()
    assert any(o.get("type") == "group_members" and o["group"] == "dev"
               for o in c.sock.sent), "应主动拉取最新群成员"
    assert c.members_list is not None and c.members_list.size() == 2, \
        c.members_list.size()
    assert "我" in c.members_list.get(0), c.members_list.get(0)
    assert "在线" in c.members_list.get(1), c.members_list.get(1)
    c.handle_msg({"type": "group_info", "group": "dev",
                  "members": ["me", "bob", "carol"]})
    assert c.members_list.size() == 3, "收到 group_info 后弹窗应自动刷新"
    assert "离线" in c.members_list.get(2), c.members_list.get(2)
    c.handle_msg({"type": "presence", "user": "carol", "online": True})
    assert "在线" in c.members_list.get(2), "在线状态变化后应刷新"
    c.members_win.destroy()
    c.handle_msg({"type": "group_info", "group": "dev",
                  "members": ["me"]})   # 窗口已关闭：不应报错
    print("✅ 群成员列表（按需拉取 / 在线状态 / 自动刷新）")

    # 9.7) 去重索引 + 会话缓存限长（长时间聊天不能越用越卡）
    c.buffers.pop("long", None)
    c.seen_ids.pop("long", None)
    c.store_message("long", "bob", "重复消息", time.time(), "user", mid=7001)
    c.store_message("long", "bob", "重复消息", time.time(), "user", mid=7001)
    assert len(c.buffers["long"]) == 1, "同 id 消息只应入库一次"
    assert c.seen_ids["long"] == {7001}, c.seen_ids["long"]
    for i in range(client.BUFFER_LIMIT + 30):
        c.store_message("long", "bob", f"第{i}条", time.time(), "user",
                        mid=8000 + i)
    buf = c.buffers["long"]
    assert len(buf) == client.BUFFER_LIMIT, f"缓存应限长: {len(buf)}"
    assert buf[-1]["text"] == f"第{client.BUFFER_LIMIT + 29}条", buf[-1]["text"]
    assert 7001 not in c.seen_ids["long"], "被丢弃消息的 id 应同步清理"
    assert 8000 + client.BUFFER_LIMIT + 29 in c.seen_ids["long"], "新消息 id 应记录"
    c.render_chat()
    root.update()
    assert len(c.seen_ids["long"]) == client.BUFFER_LIMIT, \
        f"去重索引应与缓存等长: {len(c.seen_ids['long'])}"
    c.delete_conv("long", confirm=False)
    assert "long" not in c.seen_ids, "删除会话应同时清空去重索引"
    print("✅ 消息去重索引 + 会话缓存限长（" f"{client.BUFFER_LIMIT} 条）")

    # 9.75) 增量渲染：只画可见窗口；新消息只补画新增气泡（不再整屏重绘）
    for d in (c.buffers, c.seen_ids, c.vis_count):
        d.pop("big", None)
    c.ensure_conv("big", "user")
    for i in range(250):
        c.store_message("big", "bob", f"第{i}条", time.time(), "user",
                        mid=20000 + i)
    c.select_conv("big")
    root.update()
    rows = c.chat_inner.winfo_children()
    assert len(rows) == client.VISIBLE_LIMIT + 1, \
        f"只应渲染一屏（含“载入更早”按钮）: {len(rows)}"
    assert hasattr(rows[0], "invoke"), "首项应是“载入更早的消息”按钮"
    second_row = rows[2]
    c.store_message("big", "bob", "刚刚到的新消息", time.time(), "user",
                    mid=29999)
    root.update()
    rows2 = c.chat_inner.winfo_children()
    assert len(rows2) == len(rows), f"追加后控件数应恒定: {len(rows2)}"
    assert rows2[1] is second_row, "旧气泡不应被重建（说明走了增量渲染）"
    texts = []

    def collect(w):
        try:
            texts.append(w.cget("text"))
        except Exception:
            pass
        for ch in w.winfo_children():
            collect(ch)
    collect(rows2[-1])
    assert "刚刚到的新消息" in texts, texts
    c.load_earlier("big")
    root.update()
    assert len(c.chat_inner.winfo_children()) == client.VISIBLE_LIMIT * 2 + 1, \
        "“载入更早”应多展开一屏"
    print("✅ 增量渲染（只画一屏 / 新消息只补画 / 可展开更早）")

    # 9.8) 换账号登录：不得残留上一个账号的会话数据
    c.store_message("bob", "bob", "上一个账号的悄悄话", time.time(), "user",
                    mid=41)
    assert c.buffers.get("bob"), "前置条件：当前账号有聊天记录"
    c.username = "other"
    c.main_built = False        # 模拟被顶下线后重新登录
    c.build_main()
    root.update()
    assert not c.buffers, f"换账号后不应残留旧会话: {list(c.buffers)}"
    assert not c.conv_order and not c.group_members, "换账号后应清空会话状态"
    assert list(c.presence) == ["other"], c.presence
    assert c.active is None, "换账号后不应保持上一个账号的选中会话"
    c.username = "me"           # 复原，供后续用例使用
    print("✅ 换账号登录清空上一账号数据（防串号泄露）")

    # 10) 自动重连：重连成功复用界面，不重建（保留聊天记录）
    c.sock = None
    c.start_reconnect()
    assert c.reconnecting is True, "应进入重连中状态"
    inner_before = c.conv_inner
    c.handle_msg({"type": "login_ok", "username": c.username})
    assert c.reconnecting is False, "重连成功后应清除重连状态"
    assert c.conv_inner is inner_before and c.conv_inner.winfo_exists(), \
        "重连不应重建主界面"
    c.build_main()  # 幂等：再次调用应是空操作
    assert c.conv_inner is inner_before, "build_main 应幂等"
    print("✅ 断线自动重连（复用界面、不重建）")

    # 11) 心跳：定时 ping，长时间无 pong 时判定链路已死并触发重连
    c.sock = FakeSock()
    c.closing = False
    c.sock.sent.clear()
    c.last_pong = 0
    c.ping_tick()
    assert any(o.get("type") == "ping" for o in c.sock.sent), "应定时发送 ping"
    c.handle_msg({"type": "pong", "ts": 1})
    assert c.last_pong > 0, "收到 pong 应记录时间"
    stalled = time.time() - (client.PING_TIMEOUT + 5)
    c.last_pong = stalled
    calls = []
    real_lost = c.on_conn_lost
    c.on_conn_lost = lambda: calls.append(1)
    c.ping_tick()
    c.on_conn_lost = real_lost
    assert calls, "超过 PING_TIMEOUT 未收到 pong 应触发断线处理"
    assert c.sock is None, "链路判定为死亡后应主动断开 socket"
    print("✅ 应用层心跳（定时 ping / 无 pong 判死链）")

    root.destroy()
    print("\n✅ UI 交互测试全部通过")


if __name__ == "__main__":
    main()
