"""服务器间联邦（跨服务器通信）引擎。

在同一局域网内部署多台 MoyuChat 服务器时，本模块让它们两两建立**联邦链路**，
从而实现「客户端跨服务器通信」：

  - 握手鉴权：共享密钥（MOYU_FED_SECRET），拒绝非法/未授权来源（MOYU_FED_ALLOW）。
  - 在线状态同步：链路建立后互相同步在线用户名，形成「用户名 -> 对端服务器」路由表。
  - 私聊中继：客户端发往「本服务器没有、但对端有」的用户时，由本服务器经联邦转发。
  - 发现：客户端搜索某用户名时，本服务器会向所有对端询问，汇总后回告（search_result）。
  - 容错：链路断开自动重连（指数退避）；对端不可达时消息进入发件箱，恢复后补投；
         读回执/撤回也会跨服务器同步。

配置（环境变量）：
  MOYU_SERVER_ID   本服务器 ID（默认取局域网 IP；同机多实例务必显式区分）
  MOYU_FED_PORT    联邦监听端口（默认 9100）
  MOYU_FED_BIND    联邦绑定地址（默认 0.0.0.0）
  MOYU_PEERS       出向拨号的对端列表，逗号分隔，如 10.0.0.2:9100,10.0.0.3:9100
  MOYU_FED_SECRET  共享密钥（默认 moyu-fed-secret，生产务必修改）
  MOYU_FED_ALLOW   可选的入向来源白名单（CIDR/IP，逗号分隔；为空=允许所有）

链路协议复用「4 字节长度前缀 + JSON」帧，信封带 {"fed": true, "kind": ...}。
"""

import asyncio
import json
import os
import time

from common import MAX_FRAME, lan_ip
from storage import pair_key

# 与 server.PRIVATE_LIMIT 保持一致（此处独立定义以避免与 server 形成循环 import）
PRIVATE_LIMIT = 200

OFFLINE_LIMIT = 200  # 与 server.py 保持一致：单用户离线箱上限


class _Peer:
    __slots__ = ("id", "writer", "reader", "addr", "online", "outbox", "alive")

    def __init__(self, pid, writer, reader, addr):
        self.id = pid
        self.writer = writer
        self.reader = reader
        self.addr = addr
        self.online = set()      # 该对端上的在线用户名
        self.outbox = []         # 链路断开期间排队的跨服私聊 (from, to, msg)
        self.alive = False


class Federation:
    def __init__(self, server):
        self.server = server
        self.id = os.environ.get("MOYU_SERVER_ID") or lan_ip()
        self.secret = os.environ.get("MOYU_FED_SECRET", "moyu-fed-secret")
        self.fed_port = int(os.environ.get("MOYU_FED_PORT", 9100))
        self.bind_host = os.environ.get("MOYU_FED_BIND", "0.0.0.0")
        self.peers = {}                 # peer_id -> _Peer
        self.remote = {}                # username -> peer_id（路由表）
        self.lock = asyncio.Lock()
        self.allow = self._parse_allow(os.environ.get("MOYU_FED_ALLOW", ""))
        self.peer_addrs = [a.strip() for a in
                           os.environ.get("MOYU_PEERS", "").split(",") if a.strip()]
        self._query_nonce = 0
        self.pending_peer_queries = {}      # nonce -> Future[peer_id|None]
        self.pending_client_queries = {}    # nonce -> (writer, name, set, _)
        self.loop = None

    # ---------------- 配置解析 ----------------
    @staticmethod
    def _parse_allow(s):
        if not s:
            return None
        import ipaddress
        out = []
        for item in s.split(","):
            item = item.strip()
            try:
                out.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                pass
        return out or None

    def _allowed(self, host):
        if self.allow is None:
            return True
        import ipaddress
        try:
            h = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(h in net for net in self.allow)

    # ---------------- 启动 ----------------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        self._srv = await asyncio.start_server(
            self._on_accept, self.bind_host, self.fed_port)
        print(f"[fed] 联邦监听已启动 {self.bind_host}:{self.fed_port}"
              f"（本服务器 ID={self.id}）")
        if self.peer_addrs:
            print(f"[fed] 出向对等服务器：{', '.join(self.peer_addrs)}")
        for addr in self.peer_addrs:
            self.loop.create_task(self._dialer(addr))

    # ---------------- 帧收发 ----------------
    async def _raw_send(self, writer, obj):
        try:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            if len(data) > MAX_FRAME:
                return
            writer.write(len(data).to_bytes(4, "big") + data)
            await writer.drain()
        except Exception:
            pass

    async def _raw_read(self, reader):
        try:
            h = await reader.readexactly(4)
        except Exception:
            return None
        n = int.from_bytes(h, "big")
        if n <= 0 or n > MAX_FRAME:
            return None
        try:
            p = await reader.readexactly(n)
        except Exception:
            return None
        try:
            return json.loads(p.decode("utf-8"))
        except Exception:
            return None

    # ---------------- 入向（被对端拨入） ----------------
    async def _on_accept(self, reader, writer):
        addr = writer.get_extra_info("peername")
        host = addr[0] if addr else None
        if host and not self._allowed(host):
            print(f"[fed] 拒绝未授权来源 {host}")
            try:
                writer.close()
            except Exception:
                pass
            return
        try:
            peer = await self._handshake(reader, writer, False, addr)
            if peer is None:
                try:
                    writer.close()
                except Exception:
                    pass
                return
            await self._loop(peer)
        except Exception:
            pass
        finally:
            await self._drop_peer_by_writer(writer)

    # ---------------- 出向（拨出对端） ----------------
    async def _dialer(self, addr):
        host, _, port = addr.partition(":")
        if not port:
            return
        port = int(port)
        backoff = 1
        while True:
            try:
                reader, writer = await asyncio.open_connection(host, port)
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            try:
                peer = await self._handshake(reader, writer, True, addr)
                if peer is None:
                    try:
                        writer.close()
                    except Exception:
                        pass
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                await self._loop(peer)
            except Exception:
                pass
            finally:
                await self._drop_peer_by_writer(writer)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    # ---------------- 握手（双向对称） ----------------
    async def _handshake(self, reader, writer, is_outbound, addr):
        await self._raw_send(writer, {"fed": True, "kind": "hello",
                                       "server": self.id, "secret": self.secret})
        hello = await self._raw_read(reader)
        if not hello or hello.get("kind") != "hello":
            return None
        if hello.get("secret") != self.secret:
            print(f"[fed] 拒绝 {addr}：共享密钥不匹配")
            return None
        pid = hello.get("server")
        if not pid:
            return None
        # 去重：同对端只保留一条链路（ID 较小者的出向为权威连接）
        if pid in self.peers and self.peers[pid].alive and not self._keep_conn(pid, is_outbound):
            return None
        if not self._keep_conn(pid, is_outbound):
            return None
        # 若已有一条旧链路，先关掉，再用新连接接管
        old = self.peers.pop(pid, None)
        if old:
            try:
                old.writer.close()
            except Exception:
                pass
            for u in list(self.remote.keys()):
                if self.remote[u] == pid:
                    self.remote.pop(u, None)
        peer = _Peer(pid, writer, reader, addr)
        peer.alive = True
        self.peers[pid] = peer
        # 交换在线状态快照（对端也会发一份过来）
        await self._raw_send(writer, {"fed": True, "kind": "presence",
                                       "users": list(self.server.clients.keys())})
        await self._flush_outbox(peer)
        print(f"[fed] 已与对端 {pid} 建立联邦链路（{addr}）")
        return peer

    def _keep_conn(self, pid, is_outbound):
        """ID 较小者保留出向连接，较大者保留入向连接；相等则都关。"""
        if self.id < pid:
            return is_outbound
        if self.id > pid:
            return not is_outbound
        return False

    # ---------------- 链路消息循环 ----------------
    async def _loop(self, peer):
        while True:
            msg = await self._raw_read(peer.reader)
            if msg is None:
                break
            if msg.get("fed") is not True:
                continue
            kind = msg.get("kind")
            if kind == "presence":
                async with self.lock:
                    for u in list(self.remote.keys()):
                        if self.remote[u] == peer.id:
                            self.remote.pop(u, None)
                    peer.online = set(msg.get("users", []))
                    for u in peer.online:
                        self.remote[u] = peer.id
            elif kind == "presence_add":
                u = msg.get("user")
                async with self.lock:
                    self.remote[u] = peer.id
                    peer.online.add(u)
            elif kind == "presence_del":
                u = msg.get("user")
                async with self.lock:
                    if self.remote.get(u) == peer.id:
                        self.remote.pop(u, None)
                    peer.online.discard(u)
            elif kind == "private":
                await self._recv_private(msg.get("from"), msg.get("to"), msg.get("msg"))
            elif kind == "read_receipt":
                await self._recv_read_receipt(msg.get("from"), msg.get("to"), msg.get("id"))
            elif kind == "recall":
                await self._recv_recall(msg.get("from"), msg.get("to"), msg.get("id"))
            elif kind == "query_user":
                await self._answer_query(msg.get("user"), msg.get("nonce"), peer)
            elif kind == "query_result":
                await self._recv_query_result(
                    msg.get("user"), msg.get("nonce"), msg.get("found"), peer)
            # hello 已在握手阶段处理，循环内忽略

    async def _drop_peer_by_writer(self, writer):
        pid = None
        async with self.lock:
            for k, p in list(self.peers.items()):
                if p.writer is writer:
                    pid = k
                    break
        if pid:
            async with self.lock:
                self.peers.pop(pid, None)
                for u in list(self.remote.keys()):
                    if self.remote[u] == pid:
                        self.remote.pop(u, None)
            print(f"[fed] 与对端 {pid} 的链路已断开（将自动重连）")
        try:
            writer.close()
        except Exception:
            pass

    # ---------------- 在线状态广播 ----------------
    async def note_local_online(self, user):
        for p in list(self.peers.values()):
            if p.alive:
                await self._raw_send(p.writer,
                                     {"fed": True, "kind": "presence_add", "user": user})

    async def note_local_offline(self, user):
        for p in list(self.peers.values()):
            if p.alive:
                await self._raw_send(p.writer,
                                     {"fed": True, "kind": "presence_del", "user": user})

    # ---------------- 私聊中继 ----------------
    async def deliver_or_queue(self, from_user, to_user, msg):
        """把发往 to_user 的消息经联邦投递；找到了对端则转发，链路断开则排队。"""
        async with self.lock:
            pid = self.remote.get(to_user)
        if pid and pid in self.peers and self.peers[pid].alive:
            await self._raw_send(self.peers[pid].writer,
                                 {"fed": True, "kind": "private",
                                  "from": from_user, "to": to_user, "msg": msg})
            return True
        if pid:  # 知道在对端，但链路暂时断了 -> 进发件箱，恢复后补投
            self.peers[pid].outbox.append((from_user, to_user, msg))
            return True
        # 不知道在哪个对端 -> 广播询问，短等待
        found = await self._query_remote(to_user)
        if found and found in self.peers and self.peers[found].alive:
            await self._raw_send(self.peers[found].writer,
                                 {"fed": True, "kind": "private",
                                  "from": from_user, "to": to_user, "msg": msg})
            return True
        return False

    async def _recv_private(self, from_user, to_user, msg):
        if not to_user or not isinstance(msg, dict):
            return
        # 在对端看来，from_user 是「远程用户」，to_user 是本服务器上的本地用户
        key = pair_key(from_user, to_user)
        # 与本地 do_chat 一致：写入内存历史 + 索引，保证历史补投/已读回执/撤回能定位到消息
        async with self.server.lock:
            lst = self.server.private_history.setdefault(key, [])
            lst.append(msg)
            self.server.index_put("private", key, msg)
            if len(lst) > PRIVATE_LIMIT * 2:
                self.server.index_drop(lst[:len(lst) - PRIVATE_LIMIT])
                del lst[:len(lst) - PRIVATE_LIMIT]
        self.server.store.append_private(key, msg)   # 本服务器为本地用户保留历史（落盘）
        target = self.server.clients.get(to_user)
        if target:
            await self.server.send(target, msg)
        else:
            box = self.server.offline.setdefault(to_user, [])
            box.append(msg)
            if len(box) > OFFLINE_LIMIT:
                del box[:len(box) - OFFLINE_LIMIT]
            self.server.store.set_offline(to_user, box)

    # ---------------- 读回执同步 ----------------
    async def send_read_receipt(self, from_user, to_user, mid):
        pid = self.remote.get(from_user)
        if pid and pid in self.peers and self.peers[pid].alive:
            await self._raw_send(self.peers[pid].writer,
                                 {"fed": True, "kind": "read_receipt",
                                  "from": from_user, "to": to_user, "id": mid})

    async def _recv_read_receipt(self, from_user, to_user, mid):
        # from_user=原发送者（现为本地用户），to_user=读者昵称（本服务器侧会话名）
        await self.server.send_user(from_user,
                                     {"type": "read_receipt", "target": to_user, "id": mid})

    # ---------------- 撤回同步 ----------------
    async def send_recall(self, from_user, to_user, mid):
        pid = self.remote.get(from_user)
        if pid and pid in self.peers and self.peers[pid].alive:
            await self._raw_send(self.peers[pid].writer,
                                 {"fed": True, "kind": "recall",
                                  "from": from_user, "to": to_user, "id": mid})

    async def _recv_recall(self, from_user, to_user, mid):
        # 在对端侧，from_user=原发送者（本地），to_user=撤回者昵称（会话名）
        await self.server.tombstone_remote_private(from_user, to_user, mid)

    # ---------------- 发现：客户端搜索用户名 ----------------
    async def client_query_user(self, name, writer):
        if not name:
            return
        if name in self.server.clients or name in self.server.users:
            await self.server.send(writer,
                                   {"type": "search_result",
                                    "users": [{"name": name, "remote": False}]})
            return
        if not self.peers:
            await self.server.send(writer, {"type": "search_result", "users": []})
            return
        nonce = self._next_nonce()
        self.pending_client_queries[nonce] = (writer, name, set(), None)

        async def _fire():
            await asyncio.sleep(1.0)
            cq = self.pending_client_queries.pop(nonce, None)
            if not cq:
                return
            w, _nm, res, _none = cq
            users = [{"name": u, "remote": True, "server": srv}
                     for (u, srv) in res]
            await self.server.send(w, {"type": "search_result", "users": users})

        self.loop.create_task(_fire())
        for p in list(self.peers.values()):
            if p.alive:
                await self._raw_send(p.writer,
                                     {"fed": True, "kind": "query_user",
                                      "user": name, "nonce": nonce})

    async def _answer_query(self, user, nonce, peer):
        found = (user in self.server.clients) or (user in self.server.users)
        await self._raw_send(peer.writer,
                             {"fed": True, "kind": "query_result",
                              "user": user, "nonce": nonce, "found": found,
                              "server": self.id})

    async def _query_remote(self, user):
        if not self.peers:
            return None
        nonce = self._next_nonce()
        fut = self.loop.create_future()
        self.pending_peer_queries[nonce] = fut
        for p in list(self.peers.values()):
            if p.alive:
                await self._raw_send(p.writer,
                                     {"fed": True, "kind": "query_user",
                                      "user": user, "nonce": nonce})
        try:
            return await asyncio.wait_for(fut, 1.2)
        except asyncio.TimeoutError:
            return None
        finally:
            self.pending_peer_queries.pop(nonce, None)

    async def _recv_query_result(self, user, nonce, found, peer):
        fut = self.pending_peer_queries.get(nonce)
        if fut and not fut.done() and found:
            fut.set_result(peer.id)
        cq = self.pending_client_queries.get(nonce)
        if cq and found:
            cq[2].add((user, peer.id))

    async def _flush_outbox(self, peer):
        queued = peer.outbox
        peer.outbox = []
        for (f, t, m) in queued:
            await self._raw_send(peer.writer,
                                 {"fed": True, "kind": "private",
                                  "from": f, "to": t, "msg": m})

    def _next_nonce(self):
        self._query_nonce += 1
        return self._query_nonce
