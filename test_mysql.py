"""MysqlStorage 功能测试：连真实 MySQL（默认 root/root@127.0.0.1），
用独立测试库 moyu_chat_test，验证各方法与“全量留存”行为。

运行：python test_mysql.py
"""

import os
import sys

os.environ["MOYU_STORAGE"] = "mysql"
os.environ["MOYU_DB_NAME"] = "moyu_chat_test"
os.environ.setdefault("MOYU_DB_USER", "root")
os.environ.setdefault("MOYU_DB_PASSWORD", "root")

sys.path.insert(0, os.path.dirname(__file__))
import storage as st  # noqa: E402


def reset_db():
    import pymysql
    c = pymysql.connect(host="127.0.0.1", user="root", password="root",
                        charset="utf8mb4", autocommit=True)
    with c.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS moyu_chat_test")
    c.close()


def main():
    reset_db()
    s = st.MysqlStorage()
    key = st.pair_key("alice", "bob")

    # 用户
    s.add_user("alice"); s.add_user("bob"); s.add_user("alice")  # 幂等
    users = set(s.get_users())
    assert users == {"alice", "bob"}, users
    print("PASS 用户记录（add_user 幂等 / get_users）")

    # 群成员
    s.set_group("dev", ["alice", "bob"])
    assert s.get_groups().get("dev") == ["alice", "bob"], s.get_groups()
    s.set_group("dev", ["alice", "bob", "carol"])  # 覆盖
    assert s.get_groups()["dev"] == ["alice", "bob", "carol"]
    print("PASS 群成员（set_group 覆盖 / get_groups）")

    # 群历史：灌 120 条（超过载入上限 100），验证“内存只留最近 N、DB 全留存”
    for i in range(1, 121):
        s.append_history("dev", {"id": i, "group": "dev", "from": "alice",
                                 "text": f"g{i}", "ts": float(i)})
    mem = s.get_all_history()["dev"]
    assert len(mem) == s.load_group_limit, len(mem)
    assert mem[0]["id"] == 21 and mem[-1]["id"] == 120, (mem[0]["id"], mem[-1]["id"])
    db_cnt = s._run("SELECT COUNT(*) AS c FROM group_messages", fetch=True)[0]["c"]
    assert db_cnt == 120, f"DB 应全量留存 120 条，实际 {db_cnt}"
    print(f"PASS 群历史全量留存（内存最近 {len(mem)} 条 / DB 共 {db_cnt} 条）")

    # 私聊历史
    s.append_private(key, {"id": 201, "from": "alice", "to": "bob",
                           "text": "yo", "ts": 1.0})
    s.append_private(key, {"id": 202, "from": "bob", "to": "alice",
                           "text": "hi", "ts": 2.0})
    pv = s.get_private(key)
    assert [m["id"] for m in pv] == [201, 202], pv
    assert pv[0]["text"] == "yo"
    print("PASS 私聊历史（append_private / get_private 按 id 升序）")

    # 撤回：按 id 覆盖 payload
    ok = s.replace_private(key, {"id": 202, "from": "bob", "to": "alice",
                                 "text": "", "ts": 2.0, "recalled": True})
    assert ok, "replace_private 应命中"
    pv2 = s.get_private(key)
    assert pv2[-1].get("recalled") is True and pv2[-1]["text"] == ""
    miss = s.replace_private(key, {"id": 999999, "text": ""})
    assert miss is False, "不存在的 id 不应命中"
    print("PASS 撤回覆盖（replace_private 命中/未命中）")

    # 群撤回
    ok2 = s.replace_history("dev", {"id": 120, "group": "dev", "from": "alice",
                                    "text": "", "ts": 120.0, "recalled": True})
    assert ok2
    assert s.get_all_history()["dev"][-1].get("recalled") is True
    print("PASS 群撤回覆盖（replace_history）")

    # 离线箱
    s.set_offline("bob", [{"id": 301, "from": "alice", "to": "bob",
                           "text": "offline1", "ts": 3.0}])
    assert len(s.get_offline_all().get("bob", [])) == 1
    s.set_offline("bob", [])  # 覆盖清空
    assert s.get_offline_all().get("bob", []) == []
    print("PASS 离线箱（set_offline 覆盖写入/清空）")

    print("\nOK MysqlStorage 全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("\nFAIL:", e)
        sys.exit(1)
