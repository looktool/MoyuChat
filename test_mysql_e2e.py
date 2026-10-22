"""端到端：真实 server.py + MySQL 存储。两人聊天 → 校验落库 → 重启服务器 →
从 MySQL 回放历史。使用独立端口 9107 与独立库 moyu_chat_e2e，避免影响线上。

运行：python test_mysql_e2e.py
"""

import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 9107
DB = "moyu_chat_e2e"
sys.path.insert(0, ROOT)
from common import send_obj, recv_obj  # noqa: E402


def reset_db():
    import pymysql
    c = pymysql.connect(host="127.0.0.1", user="root", password="root",
                        charset="utf8mb4", autocommit=True)
    with c.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {DB}")
    c.close()


def db_count(table):
    import pymysql
    c = pymysql.connect(host="127.0.0.1", user="root", password="root",
                        database=DB, charset="utf8mb4")
    try:
        with c.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            return cur.fetchone()[0]
    finally:
        c.close()


def start_server():
    env = dict(os.environ)
    env.update(MOYU_STORAGE="mysql", MOYU_DB_NAME=DB, MOYU_DB_USER="root",
               MOYU_DB_PASSWORD="root", MOYU_PORT=str(PORT),
               MOYU_BIND_HOST="127.0.0.1")
    return subprocess.Popen([sys.executable, "-u", "server.py"], cwd=ROOT,
                            env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.STDOUT)


def connect(name):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    s.settimeout(1.0)
    send_obj(s, {"type": "login", "username": name})
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            m = recv_obj(s)
        except socket.timeout:
            continue
        if m and m.get("type") == "login_ok":
            return s
    raise RuntimeError(f"{name} 未收到 login_ok")


def drain(s, seconds=0.6):
    out = []
    end = time.time() + seconds
    s.settimeout(0.3)
    while time.time() < end:
        try:
            m = recv_obj(s)
            if m:
                out.append(m)
        except socket.timeout:
            pass
        except OSError:
            break
    return out


def main():
    reset_db()
    srv = start_server()
    time.sleep(2.0)
    try:
        a = connect("alice")
        b = connect("bob")
        drain(a); drain(b)

        send_obj(a, {"type": "chat", "to": "bob", "text": "落库测试消息"})
        pm = next((m for m in drain(b, 1.0) if m.get("type") == "private"
                   and m.get("text") == "落库测试消息"), None)
        assert pm, "bob 未收到私聊"
        print(f"PASS 实时投递（bob 收到 id={pm['id']}）")

        send_obj(a, {"type": "group_create", "group": "e2egroup",
                     "members": ["bob"]})
        drain(a, 0.6); drain(b, 0.6)
        send_obj(a, {"type": "group_msg", "group": "e2egroup",
                     "text": "群消息落库"})
        gm = next((m for m in drain(b, 1.0) if m.get("type") == "group"
                   and m.get("text") == "群消息落库"), None)
        assert gm, "bob 未收到群消息"
        print("PASS 群消息实时投递")

        priv = db_count("private_messages")
        grp = db_count("group_messages")
        users = db_count("users")
        assert priv >= 1 and grp >= 1 and users >= 2, (priv, grp, users)
        print(f"PASS MySQL 落库（private={priv}, group={grp}, users={users}）")

        a.close(); b.close()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()

    # 重启服务器，验证从 MySQL 回放历史（重启不丢）
    srv = start_server()
    time.sleep(2.0)
    try:
        b = connect("bob")
        msgs = drain(b, 1.5)
        replay = [m for m in msgs if m.get("type") == "private"
                  and m.get("text") == "落库测试消息"]
        assert replay and replay[0].get("replay") is True, \
            f"重启后应从 MySQL 回放该私聊: {replay}"
        print("PASS 重启后从 MySQL 回放私聊历史（replay=True）")

        # 群历史同样在登录时补投（服务端对所属群回放 group_history）
        gr = [m for m in msgs if m.get("type") == "group"
              and m.get("text") == "群消息落库"]
        assert gr and gr[0].get("replay") is True, \
            f"重启后应回放群消息: {gr}"
        print("PASS 重启后回放群消息")
        b.close()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()

    print("\nOK MySQL 端到端全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("\nFAIL:", e)
        sys.exit(1)
