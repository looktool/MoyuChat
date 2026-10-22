"""压力 / 浸泡测试：多用户高并发互发消息，验证投递不丢、不重、ID 全局唯一。

覆盖点：
  1. 群聊 900 条消息：每个成员都必须“恰好收到一次”（按 id 去重校验）
  2. 私聊双向 400 条：每个方向恰好一次
  3. 中途断线重连：历史回放不得出现从未见过的 id，且回放长度受限
  4. 落盘一致性：store.json 里所有消息 id 全局唯一（客户端按 id 去重的前提）
  5. 浸泡后服务器仍能正常服务新用户

用法：python test_soak.py
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

PORT = 9105
STORE = os.path.join(tempfile.gettempdir(), "moyu_soak_store.json")
os.environ["MOYU_PORT"] = str(PORT)
os.environ["MOYU_STORE"] = STORE

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import HOST, send_obj, recv_obj  # noqa: E402

GROUP_N = 300       # 每个用户在群里发多少条
PRIV_N = 200        # 私聊每个方向多少条
PASS = 0


def assert_(cond, msg):
    global PASS
    if not cond:
        raise AssertionError("❌ " + msg)
    PASS += 1
    print("  ✓", msg)


class Client:
    """带独立读线程的测试客户端：收到的报文全部缓存下来供断言。"""

    def __init__(self, name):
        self.name = name
        self.msgs = []
        self.lock = threading.Lock()
        self.sock = socket.create_connection((HOST, PORT), timeout=10)
        self.sock.settimeout(None)
        self.alive = True
        send_obj(self.sock, {"type": "login", "username": name})
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()

    def _read_loop(self):
        while self.alive:
            try:
                m = recv_obj(self.sock)
            except Exception:
                break
            if m is None:
                break
            with self.lock:
                self.msgs.append(m)

    def snapshot(self):
        with self.lock:
            return list(self.msgs)

    def wait_for(self, pred, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred(self.snapshot()):
                return True
            time.sleep(0.05)
        return pred(self.snapshot())

    def of_type(self, t):
        return [m for m in self.snapshot() if m.get("type") == t]

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass


def duplicates(ids):
    seen, dup = set(), []
    for i in ids:
        if i in seen:
            dup.append(i)
        seen.add(i)
    return dup


def main():
    for p in (STORE, STORE + ".corrupt"):
        if os.path.exists(p):
            os.remove(p)
    proc = subprocess.Popen([sys.executable, "server.py"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.STDOUT)
    try:
        time.sleep(1.5)
        a, b, c = Client("soak_a"), Client("soak_b"), Client("soak_c")
        assert_(a.wait_for(lambda ms: any(m.get("type") == "user_list"
                                          for m in ms)), "alice 登录成功")
        assert_(b.wait_for(lambda ms: any(m.get("type") == "user_list"
                                          for m in ms)), "bob 登录成功")
        assert_(c.wait_for(lambda ms: any(m.get("type") == "user_list"
                                          for m in ms)), "carol 登录成功")

        print(f"[1] 群聊压力：3 人各发 {GROUP_N} 条，共 {GROUP_N * 3} 条")
        send_obj(a.sock, {"type": "group_create", "group": "soak",
                          "members": ["soak_b", "soak_c"]})
        ok = all(cl.wait_for(lambda ms: any(m.get("type") == "group_info"
                                            for m in ms))
                 for cl in (a, b, c))
        assert_(ok, "三方都进入群聊")
        sent_ids = set()
        t0 = time.time()
        for i in range(GROUP_N):
            for cl in (a, b, c):
                send_obj(cl.sock, {"type": "group_msg", "group": "soak",
                                   "text": f"{cl.name}-{i}"})
        total = GROUP_N * 3
        done = a.wait_for(lambda ms: len([m for m in ms
                                          if m.get("type") == "group"
                                          and not m.get("replay")]) >= total,
                          timeout=60)
        elapsed = time.time() - t0
        assert_(done, f"发送方应收到全部 {total} 条群消息回显")
        for cl in (a, b, c):
            gm = [m for m in cl.of_type("group")
                  if m.get("group") == "soak" and not m.get("replay")]
            assert_(len(gm) == total, f"{cl.name} 收到 {len(gm)}/{total} 条群消息")
            ids = [m["id"] for m in gm]
            assert_(not duplicates(ids), f"{cl.name} 未收到重复群消息 id")
            texts = {m["text"] for m in gm}
            assert_(len(texts) == total, f"{cl.name} 未丢消息（去重后 {len(texts)}）")
            sent_ids |= set(ids)
        assert_(len(sent_ids) == total, "群消息 id 全局唯一")
        print(f"      （{total} 条耗时 {elapsed:.1f}s，"
              f"{total / max(elapsed, 0.001):.0f} 条/秒）")

        print(f"[2] 私聊压力：a↔b 各 {PRIV_N} 条")
        for i in range(PRIV_N):
            send_obj(a.sock, {"type": "chat", "to": "soak_b",
                              "text": f"a->b {i}"})
            send_obj(b.sock, {"type": "chat", "to": "soak_a",
                              "text": f"b->a {i}"})
        ok = b.wait_for(lambda ms: len([m for m in ms if m.get("type") == "private"
                                        and not m.get("replay")]) >= PRIV_N * 2,
                        timeout=60)
        assert_(ok, "双向私聊全部送达")
        pa = [m for m in a.of_type("private")
              if m["from"] == "soak_a" and not m.get("replay")]
        pb = [m for m in a.of_type("private")
              if m["from"] == "soak_b" and not m.get("replay")]
        assert_(len(pa) == PRIV_N and len(pb) == PRIV_N,
                f"alice 收到 a→b {len(pa)} / b→a {len(pb)}")
        assert_(not duplicates([m["id"] for m in pa + pb]), "私聊无重复 id")

        print("[3] 断线重连：历史回放不得出现陌生 id，且长度受限")
        known = {m["id"] for m in a.of_type("group")} | \
            {m["id"] for m in a.of_type("private")}
        a.close()
        time.sleep(0.5)
        a2 = Client("soak_a")
        assert_(a2.wait_for(lambda ms: any(m.get("type") == "user_list"
                                           for m in ms)), "重连后登录成功")
        a2.wait_for(lambda ms: len([m for m in ms if m.get("replay")]) > 0)
        replays = [m for m in a2.snapshot() if m.get("replay")]
        assert_(replays, "重连后应回放历史")
        unknown = [m for m in replays if m.get("id") not in known]
        assert_(not unknown, f"回放不应出现从未见过的消息: {unknown[:2]}")
        # 回放必须受限：群聊每群 ≤ HISTORY_LIMIT(100)，私聊每个会话 ≤ PRIVATE_LIMIT(200)
        grp = [m for m in replays if m.get("type") == "group"]
        prv = [m for m in replays if m.get("type") == "private"]
        assert_(len(grp) <= 100, f"群历史回放应受 HISTORY_LIMIT 限制: {len(grp)}")
        assert_(len(prv) <= 200, f"私聊回放应受 PRIVATE_LIMIT 限制: {len(prv)}")
        assert_(len(replays) <= 100 + 200, f"回放总量应受限: {len(replays)}")
        assert_(not duplicates([m["id"] for m in replays]), "回放不得含重复 id")
        a = a2

        print("[4] 落盘一致性：所有消息 id 全局唯一")
        time.sleep(0.6)          # 等落盘（每次写入都是同步的，稍等即可）
        with open(STORE, encoding="utf-8") as f:
            data = json.load(f)
        all_ids = []
        for bucket in ("group_history", "private_history"):
            for lst in data.get(bucket, {}).values():
                all_ids += [m.get("id") for m in lst if isinstance(m, dict)]
        assert_(all_ids, "存储里应有历史消息")
        assert_(not duplicates(all_ids), "存储中消息 id 全局唯一")
        assert_(data.get("groups", {}).get("soak"), "群组已落盘")
        assert_(set(data.get("users", [])) >= {"soak_a", "soak_b", "soak_c"},
                "用户名单已落盘")

        print("[5] 浸泡后服务器仍健壮：新用户可登录、旧会话可通信")
        d = Client("soak_d")
        assert_(d.wait_for(lambda ms: any(m.get("type") == "user_list"
                                          for m in ms)), "新用户可登录")
        send_obj(a.sock, {"type": "chat", "to": "soak_d", "text": "还在吗"})
        assert_(d.wait_for(lambda ms: any(m.get("type") == "private"
                                          and m.get("text") == "还在吗"
                                          for m in ms)), "浸泡后私聊仍正常")
        send_obj(a.sock, {"type": "list_groups"})
        assert_(a.wait_for(lambda ms: any(m.get("type") == "group_list"
                                          for m in ms)), "浸泡后服务器仍响应请求")
        for cl in (a, b, c, d):
            cl.close()
        print(f"\n✅ 压力测试全部通过（{PASS} 项断言）")
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
