"""端到端验证：真实 TCP 服务器 + ws_bridge + 浏览器风格 WebSocket 客户端。

验证浏览器（WebSocket JSON 文本帧）经桥接访问现有 TCP 服务器时，登录、私聊、
群聊、已读回执、typing 等协议是否工作。不依赖 Node。

运行：
    python test_ws_bridge.py
"""

import asyncio
import json
import os
import sys

import websockets

sys.path.insert(0, os.path.dirname(__file__))
# 用独立的干净存储，避免被其他测试/历史数据污染（历史回放会干扰“首条消息”断言）
os.environ["MOYU_STORE"] = os.path.join(
    os.path.dirname(__file__), "data", "test_ws_store.json")
import server as S  # noqa: E402


async def connect(uri, user):
    ws = await websockets.connect(uri)
    await ws.send(json.dumps({"type": "login", "username": user}))
    while True:
        m = json.loads(await ws.recv())
        if m["type"] == "login_ok":
            return ws, m
        if m["type"] == "login_fail":
            raise RuntimeError(f"login_fail: {m.get('reason')}")


async def drain_until(ws, want, timeout=1.0):
    try:
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if m.get("type") == want:
                return m
    except asyncio.TimeoutError:
        return None


async def main():
    # 后端 TCP 服务器
    srv = S.ChatServer()
    tcp = await asyncio.start_server(srv.handle, "127.0.0.1", 9100)
    asyncio.create_task(srv.sweep_dead())

    # 桥接：指向本测试的后端
    import ws_bridge as W  # noqa: E402
    W.BACKEND_HOST = "127.0.0.1"
    W.BACKEND_PORT = 9100
    ws_server = await websockets.serve(W.handler, "127.0.0.1", 9106)
    uri = "ws://127.0.0.1:9106/ws"
    await asyncio.sleep(0.2)

    ok = 0
    try:
        # 登录 alice / bob
        aw, alogin = await connect(uri, "alice")
        bw, blogin = await connect(uri, "bob")
        assert alogin.get("type") == "login_ok", "应收到 login_ok"
        # 等待 user_list / group_list 等握手消息
        await asyncio.sleep(0.3)
        print("PASS 登录（login_ok + 握手消息）")

        # alice 给 bob 发私聊
        await aw.send(json.dumps({"type": "chat", "to": "bob", "text": "hi bob"}))
        bm = await drain_until(bw, "private", 1.0)
        assert bm and bm["from"] == "alice" and bm["text"] == "hi bob", bm
        print("PASS 私聊经桥接送达对端")

        # bob 已读回执回给 alice
        await bw.send(json.dumps({"type": "read", "target": "alice",
                                  "id": bm["id"], "to": "alice"}))
        am = await drain_until(aw, "read_receipt", 1.0)
        assert am and am["id"] == bm["id"], am
        print("PASS 已读回执经桥接回传")

        # 建群 + 群消息
        await aw.send(json.dumps({"type": "group_create", "group": "dev",
                                  "members": ["bob"]}))
        await drain_until(aw, "group_info", 1.0)
        await drain_until(bw, "group_info", 1.0)
        await aw.send(json.dumps({"type": "group_msg", "group": "dev",
                                  "text": "hello group"}))
        bg = await drain_until(bw, "group", 1.0)
        assert bg and bg["text"] == "hello group" and bg["group"] == "dev", bg
        print("PASS 建群 + 群消息经桥接送达成员")

        # typing 转发
        await aw.send(json.dumps({"type": "typing", "conv": "bob"}))
        bt = await drain_until(bw, "typing", 1.0)
        assert bt, f"bob 未收到 typing（收到: {bt}）"
        assert bt["from"] == "alice", bt
        print("PASS typing 经桥接转发")

        # 心跳
        await aw.send(json.dumps({"type": "ping", "ts": 1}))
        bp = await drain_until(aw, "pong", 1.0)
        assert bp, "ping 应得到 pong"
        print("PASS 心跳 ping/pong 经桥接")

        await aw.close()
        await bw.close()
        ok = 1
    finally:
        ws_server.close()
        tcp.close()

    print("✅ ws_bridge 端到端测试通过" if ok else "❌ 测试失败")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
