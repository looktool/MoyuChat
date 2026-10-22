"""MoyuChat TCP 服务器。

基于 asyncio 的 TCP 服务器，负责：
  - 维护在线用户连接
  - 私聊消息路由（点对点转发）
  - 群聊的创建、加入与广播
  - 在线用户列表 / 群组列表的同步

仅使用 Python 标准库，无需安装任何第三方依赖。
"""

import asyncio
import json
import os
import socket
import time
import uuid

import discover
from common import (PORT, BIND_HOST, HOST, MAX_FRAME, MAX_NAME, MAX_TEXT,
                    RECALL_WINDOW, PING_INTERVAL, PING_TIMEOUT, valid_name,
                    lan_ip)
from storage import create_storage, pair_key
from fed import Federation

HISTORY_LIMIT = 100  # 每个群组在内存中保留的最近消息条数
PRIVATE_LIMIT = 200  # 每个私聊会话在内存中保留的最近消息条数
READ_BATCH = 200     # 单次批量已读回执最多处理多少条（防止一次塞入海量 id）
RATE_LIMIT = 1500    # 单条连接在 RATE_WINDOW 秒内允许的报文数（防刷屏/DoS）
RATE_WINDOW = 10     # 限流窗口（秒）
# 超过这么久没收到任何报文（含心跳）就认为连接已死，主动断开
IDLE_TIMEOUT = PING_TIMEOUT + 20
OFFLINE_LIMIT = 200  # 单个用户离线消息箱的上限（超出丢弃最旧的）


def sfield(msg, key):
    """安全取出字符串字段：非字符串一律视为空，避免 .strip() 抛异常。"""
    v = msg.get(key)
    return v.strip() if isinstance(v, str) else ""





class ChatServer:
    def __init__(self):
        self.clients = {}        # username -> StreamWriter
        self.lock = asyncio.Lock()
        # 消息 id 采用全局唯一 UUID（见 _new_id）：既避免重启后重号，
        # 也避免联邦跨服务器时各服务器自增 ID 相互覆盖导致回执/撤回命中错消息。
        self.next_id = None  # 已弃用，保留仅作占位
        self.store = create_storage()   # 持久化层
        # 从持久化层恢复数据（重启不丢群/历史/离线消息）
        self.groups = {k: set(v) for k, v in self.store.get_groups().items()}
        self.group_history = {k: list(v)
                              for k, v in self.store.get_all_history().items()}
        for g in self.groups:
            self.group_history.setdefault(g, [])
        self.offline = {k: list(v)
                        for k, v in self.store.get_offline_all().items()}
        self.private_history = {k: list(v)
                                for k, v in self.store.get_all_private().items()}
        # 曾登录过的用户名：用于阻止群名抢占（否则离线用户回来时会被自己的名字
        # 建出的群“永久锁在门外”）
        self.users = set(self.store.get_users())
        # 消息 ID 采用全局唯一 UUID（见 _new_id），无需再从历史最大值续号。
        # 消息 id -> (kind, key, msg)：撤回/已读回执都要按 id 查消息，
        # 没有索引时每条回执都要扫全部历史（O(n²)），消息一多服务器就会被拖垮。
        self.msg_index = {}
        for lst in self.group_history.values():
            for m in lst:
                self.index_put("group", m.get("group"), m)
        for key, lst in self.private_history.items():
            for m in lst:
                self.index_put("private", key, m)
        # 心跳：记录每个连接最近一次收发时间，用于清理“半开”死连接
        self.activity = {}
        self.rates = {}   # 每个连接的限流计数（防刷屏/DoS）
        self.fed = Federation(self)   # 服务器间联邦（跨服务器通信）

    def _new_id(self):
        """生成全局唯一的消息 ID（UUID4 十六进制串）。

        跨服务器联邦下，各服务器若各自维护自增整数 ID，会因 ID 相互覆盖导致
        已读回执/撤回命中错误的消息；用 UUID 可从根上杜绝该问题。
        """
        return uuid.uuid4().hex

    def index_put(self, kind, key, msg):
        mid = msg.get("id") if isinstance(msg, dict) else None
        if isinstance(mid, (int, str)):
            self.msg_index[mid] = (kind, key, msg)

    def index_drop(self, msgs):
        """消息被裁剪出历史时，同步移除索引，避免索引无限膨胀。"""
        for m in msgs:
            mid = m.get("id") if isinstance(m, dict) else None
            if isinstance(mid, (int, str)) and \
                    self.msg_index.get(mid, (None, None, m))[2] is m:
                self.msg_index.pop(mid, None)

    def peers_of(self, username):
        """返回与该用户有过私聊会话的对端用户名集合。"""
        peers = set()
        for key in self.private_history:
            parts = key.split("\x1f")
            if len(parts) == 2 and username in parts:
                peers.add(parts[0] if parts[1] == username else parts[1])
        return peers

    # ---------- 底层发送 ----------
    async def send(self, writer, obj):
        try:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            writer.write(len(data).to_bytes(4, "big") + data)
            await writer.drain()
        except Exception:
            pass

    async def send_user(self, username, obj):
        """按用户名发送（对象可能已断开，故用 get 兜底，避免 KeyError）。"""
        w = self.clients.get(username)
        if w is not None:
            await self.send(w, obj)

    async def broadcast_user_list(self):
        obj = {"type": "user_list", "users": list(self.clients.keys())}
        for w in list(self.clients.values()):
            await self.send(w, obj)

    async def broadcast_presence(self, user, online):
        obj = {"type": "presence", "user": user, "online": online}
        for w in list(self.clients.values()):
            await self.send(w, obj)

    def allow(self, writer):
        """简易滑动窗口限流：单条连接在 RATE_WINDOW 秒内最多 RATE_LIMIT 个报文。"""
        now = time.time()
        start, count = self.rates.get(writer, (now, 0))
        if now - start >= RATE_WINDOW:
            start, count = now, 0
        count += 1
        self.rates[writer] = (start, count)
        return count <= RATE_LIMIT

    async def sweep_dead(self):
        """清理长时间无心跳的连接：否则掉电/断网的用户会一直显示为“在线”。"""
        while True:
            await asyncio.sleep(max(5, IDLE_TIMEOUT // 3))
            deadline = time.time() - IDLE_TIMEOUT
            for w in list(self.activity):
                if self.activity.get(w, time.time()) < deadline:
                    try:
                        w.close()   # 触发 handle 正常收尾（下线广播等）
                    except Exception:
                        pass

    # ---------- 连接主循环 ----------
    async def handle(self, reader, writer):
        username = None
        # 开启 TCP keepalive，便于及时发现“半开”死连接
        try:
            rsock = writer.get_extra_info("socket")
            if rsock is not None:
                rsock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass
        try:
            while True:
                header = await reader.readexactly(4)
                length = int.from_bytes(header, "big")
                if length <= 0 or length > MAX_FRAME:  # 非法长度，断开连接
                    break
                payload = await reader.readexactly(length)
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue  # 坏帧：忽略，不断开
                if not isinstance(msg, dict):
                    continue
                cmd = msg.get("type")
                # 心跳：ping 立即回 pong（客户端据此判断连接是否还活着），
                # 并记录活跃时间；服务端也有后台任务清理长期无响应的连接。
                self.activity[writer] = time.time()
                if cmd == "ping":
                    await self.send(writer, {"type": "pong",
                                             "ts": msg.get("ts")})
                    continue
                if cmd != "login" and not self.allow(writer):
                    # 单条连接刷屏保护：超限只提示，不断开（正常用户不会触发）
                    await self.send(writer, {
                        "type": "system",
                        "text": "发送过于频繁，请稍后再试"})
                    continue

                if cmd == "login":
                    # 一个连接只能登录一次：否则同一 socket 可登记多个身份，
                    # 断线时只清理最后一个，其余会变成“幽灵在线用户”。
                    if username is None:
                        username = await self.do_login(msg, writer)
                    continue
                elif username is None:
                    continue
                elif cmd == "chat":
                    await self.do_chat(username, msg)
                elif cmd == "group_create":
                    await self.do_group_create(username, msg)
                elif cmd == "group_join":
                    await self.do_group_join(username, msg)
                elif cmd == "group_leave":
                    await self.do_group_leave(username, msg)
                elif cmd == "group_msg":
                    await self.do_group_msg(username, msg)
                elif cmd == "recall":
                    await self.do_recall(username, msg)
                elif cmd == "read":
                    await self.do_read(username, msg)
                elif cmd == "typing":
                    await self.do_typing(username, msg)
                elif cmd == "list_users":
                    await self.send(writer, {"type": "user_list", "users": list(self.clients.keys())})
                elif cmd == "list_groups":
                    await self.send(writer, {"type": "group_list",
                                             "groups": self.groups_of(username)})
                elif cmd == "list_all_groups":  # “发现群聊”：所有公开群 + 是否已加入
                    await self.send(writer, {
                        "type": "all_groups",
                        "groups": self.all_groups_of(username)})
                elif cmd == "query_user":  # 跨服务器发现：搜索某用户名所在的服务器
                    await self.fed.client_query_user(sfield(msg, "user"), writer)
                elif cmd == "group_members":  # 按需拉取群成员（成员列表弹窗用）
                    await self.do_group_members(username, sfield(msg, "group"))
        except (asyncio.IncompleteReadError, ConnectionResetError,
                ConnectionAbortedError, OSError):
            pass
        except Exception:
            pass  # 单个连接的任何异常都不应影响服务器整体
        finally:
            self.activity.pop(writer, None)
            self.rates.pop(writer, None)
            if username:
                stale = False
                async with self.lock:
                    if self.clients.get(username) is writer:
                        self.clients.pop(username, None)
                    else:
                        stale = True  # 已被新连接接管，勿误删
                if not stale:
                    await self.broadcast_user_list()
                    await self.broadcast_presence(username, False)  # 广播“我下线了”
                    await self.fed.note_local_offline(username)  # 通知联邦对端：本服务器该用户下线
                    print(f"[-] {username} 断开连接")
            try:
                writer.close()  # 无论何种原因退出，都关闭连接，避免“僵尸连接”
            except Exception:
                pass

    # ---------- 业务处理 ----------
    async def do_login(self, msg, writer):
        name = sfield(msg, "username")
        if not name:
            await self.send(writer, {"type": "login_fail",
                                     "reason": "用户名不能为空"})
            return None
        if not valid_name(name):
            await self.send(writer, {
                "type": "login_fail",
                "reason": f"用户名不合法：1~{MAX_NAME} 个可见字符"})
            return None
        if name in self.groups:
            # 客户端以“名字”作为会话键，用户与群组同名会导致会话串台
            await self.send(writer, {
                "type": "login_fail",
                "reason": "该名称已被群组占用，请更换用户名"})
            return None
        async with self.lock:
            old = self.clients.get(name)
            self.clients[name] = writer
        if old is not None and old is not writer:
            # 同名连接重登（如断线自动重连）：踢掉旧连接，由新连接接管。
            # 必须显式告知旧连接“你被顶掉了”，否则两个客户端会互相重连、互相踢，
            # 陷入无限拉锯（旧连接只会看到普通的 EOF，以为只是网络抖动）。
            await self.send(old, {"type": "kicked",
                                  "reason": "该账号已在其他窗口登录"})
            try:
                old.close()
            except Exception:
                pass
        self.users.add(name)
        self.store.add_user(name)  # 记住这个名字，避免日后被同名群抢占
        await self.send(writer, {"type": "login_ok", "username": name})
        await self.send(writer, {"type": "user_list", "users": list(self.clients.keys())})
        await self.send(writer, {"type": "group_list",
                                 "groups": self.groups_of(name)})
        # 投递离线期间缓存的私聊消息
        async with self.lock:
            inbox = self.offline.pop(name, [])
        self.store.set_offline(name, [])  # 落盘清空离线箱
        for m in inbox:
            await self.send(writer, m)
        # 补投私聊历史（含离线消息，客户端按 id 去重；用于重启后恢复 1:1 聊天记录）
        async with self.lock:
            my_peers = self.peers_of(name)
        for peer in my_peers:
            for m in self.private_history.get(pair_key(name, peer), [])[-PRIVATE_LIMIT:]:
                await self.send(writer, {**m, "replay": True})
        # 补投离线期间错过的群消息（按 id 去重，客户端不会重复显示）
        async with self.lock:
            my_groups = {g: list(mem) for g, mem in self.groups.items()
                         if name in mem}
        for g, mem in my_groups.items():
            for m in self.group_history.get(g, [])[-HISTORY_LIMIT:]:
                await self.send(writer, {**m, "replay": True})
            # 同步群成员信息（会话头可直接显示人数）
            await self.send(writer, {"type": "group_info",
                                     "group": g, "members": mem})
        await self.broadcast_user_list()  # 通知其他用户更新在线列表
        await self.broadcast_presence(name, True)  # 广播“我上线了”
        await self.fed.note_local_online(name)     # 通知联邦对端：本服务器新增在线用户
        print(f"[+] {name} 已连接")
        return name

    async def do_chat(self, username, msg):
        to = sfield(msg, "to")
        text = sfield(msg, "text")
        if not to or not text or to == username:
            return
        if not valid_name(to):
            return  # 非法收件人名：会变成对不上任何人的垃圾历史，直接忽略
        if len(text) > MAX_TEXT:
            await self.send_user(username, {
                "type": "system", "conv": to,
                "text": f"消息过长（上限 {MAX_TEXT} 字），未发送"})
            return
        # 私聊历史落盘（重启后仍可恢复 1:1 聊天记录；跨服时发送方服务器也保留己方历史）
        key = pair_key(username, to)
        async with self.lock:
            out = {"type": "private", "id": self._new_id(), "from": username,
                   "to": to, "text": text, "ts": time.time()}
            lst = self.private_history.setdefault(key, [])
            lst.append(out)
            self.index_put("private", key, out)
            if len(lst) > PRIVATE_LIMIT * 2:
                self.index_drop(lst[:len(lst) - PRIVATE_LIMIT])
                del lst[:len(lst) - PRIVATE_LIMIT]
        self.store.append_private(key, out)
        if to in self.clients:
            await self.send(self.clients[to], out)     # 本服务器在线 -> 直发
            await self.send_user(username, out)        # 回显发送方
        elif to in self.users:
            # 本服务器注册过但当前离线 -> 离线箱（与原行为一致）
            box = self.offline.setdefault(to, [])
            box.append(out)
            if len(box) > OFFLINE_LIMIT:  # 只为防内存/磁盘无限增长，丢弃最旧的
                del box[:len(box) - OFFLINE_LIMIT]
            self.store.set_offline(to, box)
            await self.send_user(username, out)        # 回显发送方
        else:
            # 目标不在本服务器：尝试经联邦投递到其对端服务器（或排队/报错）
            ok = await self.fed.deliver_or_queue(username, to, out)
            if ok:
                await self.send_user(username, out)    # 回显发送方（含 id，单勾）
            else:
                await self.send_user(username, {
                    "type": "system", "conv": to,
                    "text": f"「{to}」当前不可达（本服务器及所有已连接的对等服务器"
                            f"均未找到该用户）"})

    async def do_group_create(self, username, msg):
        g = sfield(msg, "group")
        if not g:
            return
        if not valid_name(g):
            await self.send_user(username, {
                "type": "system",
                "text": f"群名不合法：1~{MAX_NAME} 个可见字符"})
            return
        raw = msg.get("members")
        members = [m.strip() for m in raw if isinstance(m, str) and m.strip()] \
            if isinstance(raw, list) else []
        bad = [m for m in members if not valid_name(m)]  # 非法成员名：回告而不是静默丢
        members = [m for m in members if valid_name(m)]
        if g not in self.groups and (g in self.users or g in self.clients):
            # 与用户重名会让双方客户端的同一个会话键串台，直接拒绝；
            # 连离线但曾登录过的用户名也一并保护，否则对方回来就登不上了
            await self.send_user(username, {
                "type": "system",
                "text": f"群名「{g}」已被用户名占用，请换一个群名"})
            return
        # 群名与成员名不能同名（客户端用同一个名字做会话键，会串台）
        clash = [m for m in members if m in self.groups]
        members = [m for m in members if m not in self.groups]
        async with self.lock:
            exists = g in self.groups
            is_member = exists and username in self.groups[g]
            if exists and not is_member:
                # 已存在的群只有成员才能往里拉人，否则任何人都能“自封入群”并翻看群聊
                need_join = True
            else:
                need_join = False
                if not exists:
                    self.groups[g] = set()
                    self.group_history[g] = []
                self.groups[g].add(username)
                added = []
                for m in members:
                    if m in self.groups[g]:
                        continue
                    # 允许把离线用户也加入群组（其上线后可收到群历史）
                    self.groups[g].add(m)
                    if m in self.clients:
                        added.append(m)
                snapshot = list(self.groups[g])
        if need_join:
            await self.send_user(username, {
                "type": "system",
                "text": f"你不是群聊「{g}」的成员，无法邀请他人；"
                        f"可在“发现群聊”里加入"})
            return
        for m in bad + clash:   # 明确回告被忽略的名字，避免用户以为已经拉进来了
            why = "不是合法用户名" if m in bad else "与已有群名相同"
            await self.send_user(username, {
                "type": "system", "conv": g,
                "text": f"已忽略成员「{m}」（{why}）"})
        self.store.set_group(g, snapshot)  # 落盘
        await self.send_user(username,
                             {"type": "system", "text": f"已创建/加入群组 {g}",
                              "conv": g})
        await self.send_user(username,
                             {"type": "group_list",
                              "groups": self.groups_of(username)})
        for m in added:
            await self.send_user(m,
                                 {"type": "system",
                                  "text": f"你已被加入群组 {g}", "conv": g})
            await self.send_user(m, {"type": "group_list",
                                     "groups": self.groups_of(m)})
        await self.broadcast_group_info(g)

    def groups_of(self, username):
        """返回该用户所属的群组名（仅自己加入的，避免展示无法发送的群）。"""
        return [g for g, mem in self.groups.items() if username in mem]

    async def do_group_members(self, username, g):
        """按需返回群成员（只允许群内成员查询，避免泄露无关群的成员）。"""
        if not g:
            return
        async with self.lock:
            members = list(self.groups.get(g, set()))
        if username not in members:
            await self.send_user(username, {
                "type": "system", "text": f"你不在群聊「{g}」中"})
            return
        await self.send_user(username, {"type": "group_info",
                                        "group": g, "members": members})

    def all_groups_of(self, username):
        """全部群组概况（用于“发现群聊”）：名称 / 人数 / 本人是否已加入。"""
        return [{"name": g, "count": len(mem), "joined": username in mem}
                for g, mem in sorted(self.groups.items())]

    async def broadcast_group_info(self, g):
        async with self.lock:
            members = list(self.groups.get(g, set()))
        obj = {"type": "group_info", "group": g, "members": members}
        for m in members:
            w = self.clients.get(m)
            if w:
                await self.send(w, obj)

    async def do_group_join(self, username, msg):
        g = sfield(msg, "group")
        if not g:
            return
        async with self.lock:
            if g not in self.groups:
                hist = None      # 不在锁内 await，避免拖住其他客户端
            else:
                self.groups[g].add(username)
                hist = list(self.group_history[g][-HISTORY_LIMIT:])
        if hist is None:
            await self.send_user(username, {"type": "system",
                                            "text": f"群组 {g} 不存在"})
            return
        self.store.set_group(g, list(self.groups[g]))  # 落盘
        for h in hist:  # 发送历史消息
            await self.send_user(username, {**h, "replay": True})
        await self.send_user(username,
                             {"type": "system", "text": f"已加入群组 {g}",
                              "conv": g})
        await self.broadcast_group_info(g)  # 同步最新成员列表

    async def do_group_leave(self, username, msg):
        g = sfield(msg, "group")
        if not g:
            return
        async with self.lock:
            if g in self.groups:
                self.groups[g].discard(username)
                members = list(self.groups[g])
            else:
                members = []  # 群不存在：不要落盘，避免产生“幽灵空群”
        if g in self.groups:
            self.store.set_group(g, members)  # 落盘
        await self.send_user(username, {"type": "left_group", "group": g})
        # 通知其他成员刷新人数
        for m in members:
            w = self.clients.get(m)
            if w:
                await self.send(w, {"type": "group_info", "group": g,
                                    "members": members})

    async def do_group_msg(self, username, msg):
        g = sfield(msg, "group")
        text = sfield(msg, "text")
        if not g or not text:
            return
        if len(text) > MAX_TEXT:
            await self.send_user(username, {
                "type": "system", "conv": g,
                "text": f"消息过长（上限 {MAX_TEXT} 字），未发送"})
            return
        async with self.lock:
            if g not in self.groups or username not in self.groups[g]:
                out = None          # 非群成员不得发言
            else:
                out = {"type": "group", "id": self._new_id(), "group": g,
                       "from": username, "text": text, "ts": time.time()}
                members = list(self.groups[g])
                self.group_history[g].append(out)
                self.index_put("group", g, out)
                if len(self.group_history[g]) > HISTORY_LIMIT * 2:
                    self.index_drop(self.group_history[g]
                                    [:len(self.group_history[g]) - HISTORY_LIMIT])
                    self.group_history[g] = \
                        self.group_history[g][-HISTORY_LIMIT:]
        if out is None:
            # 静默丢弃会让用户以为消息已经发出去了，必须明确告知
            await self.send_user(username, {
                "type": "system",
                "text": f"你已不在群聊「{g}」中，消息未发送"})
            return
        self.store.append_history(g, out)  # 落盘（内部限长）
        for m in members:  # 广播给群内所有成员（含发送者）
            w = self.clients.get(m)
            if w:
                await self.send(w, out)

    def find_message(self, mid):
        """按 id 查找消息，返回 ("group", 群名, msg) / ("private", 会话键, msg)。

        走索引 O(1)；索引缺失（如旧存档未建索引）时退回全量扫描兜底。
        """
        if not isinstance(mid, (int, str)):
            return None
        hit = self.msg_index.get(mid)
        if hit is not None:
            return hit
        for g, lst in self.group_history.items():
            for m in lst:
                if m.get("id") == mid:
                    return ("group", g, m)
        for key, lst in self.private_history.items():
            for m in lst:
                if m.get("id") == mid:
                    return ("private", key, m)
        return None

    async def do_read(self, username, msg):
        """转发“已读”回执给原消息发送者。

        回执的 target 由服务端按消息本身推导：客户端只知道“自己这边的会话名”
        （私聊即对方昵称），直接透传会让原发送者在错误的会话里找不到这条消息，
        导致“已读”永远不生效。同时校验回执方确实读过这条消息，防止伪造回执。
        支持批量（打开一个积压很多未读的会话时一次回执即可，避免报文风暴）。
        """
        if isinstance(msg.get("ids"), list):  # 批量回执
            mids = [i for i in msg["ids"] if isinstance(i, (int, str))][:READ_BATCH]
        elif isinstance(msg.get("id"), (int, str)):
            mids = [msg["id"]]
        else:
            return
        for mid in mids:
            await self.forward_read(username, mid)

    async def forward_read(self, username, mid):
        found = self.find_message(mid)
        if found is None:
            return  # 消息不存在（可能已被清理），静默忽略
        kind, key, orig = found
        sender = orig.get("from")
        if not sender or sender == username:
            return  # 不会给自己发回执
        if kind == "group":
            async with self.lock:
                if username not in self.groups.get(key, set()):
                    return  # 非群成员不得回执
            target = key
        else:
            if orig.get("to") != username:
                return  # 只有接收方本人才能回执
            target = username  # 对方（原发送者）那边的会话名就是回执方昵称
        # 原发送者若在联邦对端（跨服务器消息），把已读回执经联邦转过去
        if kind == "private" and sender in self.fed.remote:
            await self.fed.send_read_receipt(sender, target, mid)
            return
        await self.send_user(sender, {"type": "read_receipt",
                                      "target": target, "id": mid})

    async def do_recall(self, username, msg):
        """撤回自己发出的消息（限 RECALL_WINDOW 内），并通知相关方删除。

        实现为“墓碑”式覆盖：把原消息文本清空并标记 recalled=True，
        这样历史补投时对方也不会再看到原文。
        """
        mid = msg.get("id")
        if not isinstance(mid, (int, str)) or not mid:
            return
        now = time.time()
        target = None      # ("group", 群名) 或 ("private", 会话键)
        tomb = None
        members = []
        async with self.lock:
            for key, lst in self.private_history.items():
                for i, m in enumerate(lst):
                    if m.get("id") == mid and m.get("from") == username:
                        if now - (m.get("ts") or 0) > RECALL_WINDOW:
                            return  # 超时不可撤回
                        parts = key.split("\x1f")
                        peer = parts[0] if parts[-1] == username else parts[-1]
                        tomb = {**m, "text": "", "recalled": True}
                        lst[i] = tomb
                        self.index_put("private", key, tomb)  # 索引同步为墓碑
                        target = ("private", peer)
                        break
                if tomb:
                    break
            if tomb is None:  # 不在私聊里，找群历史
                for g, lst in self.group_history.items():
                    if username not in self.groups.get(g, set()):
                        continue
                    for i, m in enumerate(lst):
                        if m.get("id") == mid and m.get("from") == username:
                            if now - (m.get("ts") or 0) > RECALL_WINDOW:
                                return
                            tomb = {**m, "text": "", "recalled": True,
                                    "group": g}
                            lst[i] = tomb
                            self.index_put("group", g, tomb)  # 索引同步为墓碑
                            target = ("group", g)
                            members = list(self.groups.get(g, set()))
                            break
                    if tomb:
                        break
        if tomb is None:
            return
        if target[0] == "private":
            peer = target[1]
            key = pair_key(username, peer)
            self.store.replace_private(key, tomb)
            # 私聊双方看到的“会话名”是对方的昵称：给谁的报文就得写谁那边的名字，
            # 否则收方拿着不认识的名字去查缓存会查不到（撤回提示不生效）。
            await self.send_user(peer, {"type": "recalled", "id": mid,
                                        "conv": username, "from": username})
            await self.send_user(username, {"type": "recalled", "id": mid,
                                            "conv": peer, "from": username})
            # 若被撤回消息的原发送者在对端服务器，同步撤回（跨服务器）
            if self.fed.remote.get(peer):
                await self.fed.send_recall(peer, username, mid)
        else:
            self.store.replace_history(target[1], tomb)
            out = {"type": "recalled", "id": mid, "conv": target[1],
                   "group": target[1], "from": username}
            for m in members:
                await self.send_user(m, out)

    async def do_typing(self, username, msg):
        """转发“正在输入”状态给对方（私聊）或群成员。"""
        conv = sfield(msg, "conv")
        if not conv:
            return
        out = {"type": "typing", "conv": conv, "from": username}
        if msg.get("group"):  # 群聊：广播给除自己外的成员
            async with self.lock:
                if username not in self.groups.get(conv, set()):
                    return  # 非群成员不得往群里广播“正在输入”
                members = list(self.groups.get(conv, set()))
            for m in members:
                if m != username:
                    w = self.clients.get(m)
                    if w:
                        await self.send(w, out)
        else:  # 私聊：仅发给对方
            w = self.clients.get(conv)
            if w:
                await self.send(w, out)

    async def tombstone_remote_private(self, from_user, to_user, mid):
        """跨服务器撤回：在对端服务器上把本地用户的「发出/收到」消息墓碑化。

        from_user 为原发送者（本服务器上的本地用户），to_user 为读者昵称
        （本服务器侧会话名）。撤回后通知本地 from_user 刷新对应会话。
        """
        now = time.time()
        async with self.lock:
            key = pair_key(from_user, to_user)
            lst = self.private_history.get(key)
            if not lst:
                return
            for i, m in enumerate(lst):
                if m.get("id") == mid and m.get("from") == from_user:
                    if now - (m.get("ts") or 0) > RECALL_WINDOW:
                        return
                    tomb = {**m, "text": "", "recalled": True}
                    lst[i] = tomb
                    self.index_put("private", key, tomb)  # 索引同步为墓碑
                    break
            else:
                return
        self.store.replace_private(key, tomb)
        await self.send_user(from_user, {"type": "recalled", "id": mid,
                                          "conv": to_user, "from": from_user})


async def main():
    server = ChatServer()
    srv = await asyncio.start_server(server.handle, BIND_HOST, PORT,
                                    reuse_address=True)
    asyncio.create_task(server.sweep_dead())  # 清理无心跳的死连接
    ip = lan_ip()
    print(f"MoyuChat 服务器已启动，监听 {BIND_HOST}:{PORT}")
    print(f"局域网多人聊天：其他人用服务器地址 {ip}:{PORT} 连接"
          f"（桌面客户端登录框填写，网页端访问 http://{ip}:5173）")
    # UDP 广播自动发现：桌面客户端可免手输 IP（浏览器无法发 UDP，仅桌面端受益）
    ws_port = int(os.environ.get("MOYU_WS_PORT", 9106))
    discover.run_responder_thread(PORT, name="MoyuChat", ws_port=ws_port)
    print(f"已开启 UDP 自动发现（端口 {discover.DISCOVERY_PORT}）："
          f"桌面客户端登录可一键扫描")
    # 服务器间联邦（跨服务器通信）：启动监听 + 出向拨号对等服务器
    await server.fed.start()
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n服务器已停止")
