"""数据持久化层。

设计目标：
  - 默认使用本地 JSON 文件存储（零依赖，开箱即用），保证服务端重启后
    群组、群历史、离线私聊消息不丢失。
  - 通过统一的 ``BaseStorage`` 接口预留 MySQL 实现：将来只需新增一个
    ``MysqlStorage`` 子类并实现相同方法，再把 ``create_storage()`` 的返回值
    切换为它即可，server.py 无需改动（依赖倒置）。

数据结构（JSON 文件）：
  {
    "users":           [用户名...],                    # 曾登录过的用户名（防群名抢占）
    "groups":          { 群名: [成员...] },
    "group_history":   { 群名: [消息dict...] },        # 最近 N 条
    "private_history": { "a\x1fb": [私聊消息dict...] }, # 键为双方用户名排序后拼接
    "offline":         { 用户名: [私聊消息dict...] }
  }
"""

import json
import os
import threading
from abc import ABC, abstractmethod

# 私聊会话键分隔符（用不可见控制符，避免与用户名中的普通字符冲突）
PAIR_SEP = "\x1f"


def pair_key(a, b):
    """把两个用户名归一化成与顺序无关的会话键。"""
    return PAIR_SEP.join(sorted([a, b]))


class BaseStorage(ABC):
    """持久化抽象接口（预留 MySQL 等实现）。所有方法线程安全由子类保证。"""

    @abstractmethod
    def get_groups(self):
        """返回 {群名: [成员...]}。"""

    @abstractmethod
    def set_group(self, name, members):
        """覆盖写入某个群的成员列表。"""

    @abstractmethod
    def get_all_history(self):
        """返回 {群名: [消息dict...]}。"""

    @abstractmethod
    def append_history(self, name, msg):
        """向某群历史追加一条消息（实现方负责限长）。"""

    @abstractmethod
    def get_offline_all(self):
        """返回 {用户名: [私聊消息dict...]}。"""

    @abstractmethod
    def set_offline(self, user, messages):
        """覆盖写入某用户的离线私聊消息列表。"""

    @abstractmethod
    def get_all_private(self):
        """返回 {会话键: [私聊消息dict...]}，会话键由 pair_key() 生成。"""

    @abstractmethod
    def append_private(self, key, msg):
        """向某私聊会话历史追加一条消息（实现方负责限长）。"""

    @abstractmethod
    def get_private(self, key):
        """返回某个私聊会话的历史消息列表。"""

    @abstractmethod
    def replace_history(self, name, msg):
        """按消息 id 覆盖某群历史中的一条消息（用于撤回）；返回是否命中。"""

    @abstractmethod
    def replace_private(self, key, msg):
        """按消息 id 覆盖某私聊会话中的一条消息（用于撤回）；返回是否命中。"""

    @abstractmethod
    def get_users(self):
        """返回曾登录过的用户名列表。"""

    @abstractmethod
    def add_user(self, name):
        """记录一个用户名（幂等）。"""


class JsonStorage(BaseStorage):
    """基于本地 JSON 文件的实现（默认）。"""

    def __init__(self, path=None):
        self.path = path or os.environ.get("MOYU_STORE", "data/store.json")
        self.lock = threading.Lock()
        self.data = {"users": [], "groups": {}, "group_history": {},
                     "private_history": {}, "offline": {}}
        self._load()
        self._sanitize()  # 字段兜底 / 清洗脏结构，兼容旧文件与人为改坏的文件

    def _sanitize(self):
        users = self.data.get("users")
        self.data["users"] = [u for u in users if isinstance(u, str)] \
            if isinstance(users, list) else []

        """把读到的数据修成期望形状 dict[str, list]，避免脏数据引发后续异常。"""
        for key in ("groups", "group_history", "private_history", "offline"):
            bucket = self.data.get(key)
            if not isinstance(bucket, dict):
                bucket = {}
            clean = {}
            for k, v in bucket.items():
                if not isinstance(k, str):
                    continue
                if key == "groups":
                    clean[k] = [m for m in v if isinstance(m, str)] \
                        if isinstance(v, list) else []
                else:
                    clean[k] = [m for m in v if isinstance(m, dict)] \
                        if isinstance(v, list) else []
            self.data[key] = clean

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update({k: loaded.get(k, {}) for k in
                                  ("groups", "group_history",
                                   "private_history", "offline")})
                self.data["users"] = loaded.get("users", [])
        except Exception as e:
            # 损坏则用空数据继续，但先把坏文件改名备份，避免被下次保存覆盖
            print(f"[!] 存储文件损坏，已忽略并备份: {self.path} ({e})")
            try:
                os.replace(self.path, self.path + ".corrupt")
            except Exception:
                pass

    def _save(self):
        """原子落盘；失败只告警不抛出，避免磁盘异常打断在线聊天。"""
        try:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)  # 原子替换，避免写一半崩溃损坏
        except Exception as e:
            print(f"[!] 存储写入失败（已忽略本次落盘）: {e}")
            try:
                if os.path.exists(self.path + ".tmp"):
                    os.remove(self.path + ".tmp")
            except Exception:
                pass

    def get_groups(self):
        with self.lock:
            return {k: list(v) for k, v in self.data["groups"].items()}

    def set_group(self, name, members):
        with self.lock:
            self.data["groups"][name] = list(members)
            self._save()

    def get_all_history(self):
        with self.lock:
            return {k: list(v) for k, v in self.data["group_history"].items()}

    def append_history(self, name, msg, limit=100):
        with self.lock:
            lst = self.data["group_history"].setdefault(name, [])
            lst.append(msg)
            if len(lst) > limit:
                del lst[:len(lst) - limit]
            self._save()

    def get_offline_all(self):
        with self.lock:
            return {k: list(v) for k, v in self.data["offline"].items()}

    def set_offline(self, user, messages):
        with self.lock:
            self.data["offline"][user] = list(messages)
            self._save()

    def get_all_private(self):
        with self.lock:
            return {k: list(v)
                    for k, v in self.data["private_history"].items()}

    def append_private(self, key, msg, limit=200):
        with self.lock:
            lst = self.data["private_history"].setdefault(key, [])
            lst.append(msg)
            if len(lst) > limit:
                del lst[:len(lst) - limit]
            self._save()

    def get_private(self, key):
        with self.lock:
            return list(self.data["private_history"].get(key, []))

    def _replace(self, bucket, key, msg):
        """把 bucket[key] 中 id 与 msg 相同的消息替换为 msg。"""
        if not isinstance(bucket.get(key), list):
            return False
        for i, old in enumerate(bucket[key]):
            if old.get("id") == msg.get("id"):
                bucket[key][i] = msg
                self._save()
                return True
        return False

    def replace_history(self, name, msg):
        with self.lock:
            return self._replace(self.data["group_history"], name, msg)

    def replace_private(self, key, msg):
        with self.lock:
            return self._replace(self.data["private_history"], key, msg)

    def get_users(self):
        with self.lock:
            return list(self.data["users"])

    def add_user(self, name):
        with self.lock:
            if name not in self.data["users"]:
                self.data["users"].append(name)
                self._save()


class MysqlStorage(BaseStorage):
    """MySQL 实现（PyMySQL）。数据库中**全量留存**所有聊天记录。

    启用：``pip install PyMySQL`` 并设置环境变量 ``MOYU_STORAGE=mysql``。
    连接参数（均有默认值）：``MOYU_DB_HOST/PORT/USER/PASSWORD/NAME``
    （默认 127.0.0.1:3306，用户 root，密码 root，库名 moyu_chat）。

    与 JsonStorage 的行为差异：启动时只把每个会话最近若干条载入内存
    （见 load_private_limit / load_group_limit），但 append 不裁剪，
    因此 MySQL 中保留全部历史，可供日后用 SQL 查询/统计。
    """

    def __init__(self, **overrides):
        try:
            import pymysql
        except ImportError as e:  # 明确提示，避免 import 时炸掉整个服务端
            raise RuntimeError(
                "使用 MySQL 存储需先安装依赖：pip install PyMySQL") from e
        self._pymysql = pymysql
        cfg = dict(
            host=os.environ.get("MOYU_DB_HOST", "127.0.0.1"),
            port=int(os.environ.get("MOYU_DB_PORT", "3306")),
            user=os.environ.get("MOYU_DB_USER", "root"),
            password=os.environ.get("MOYU_DB_PASSWORD", "root"),
            database=os.environ.get("MOYU_DB_NAME", "moyu_chat"),
            charset=os.environ.get("MOYU_DB_CHARSET", "utf8mb4"),
        )
        cfg.update(overrides)
        self.cfg = cfg
        self.lock = threading.Lock()
        # 启动时只载入每个会话最近若干条到内存（与 JSON 版一致），
        # 但数据库中保留全部历史（append 不做裁剪），满足“聊天记录全留存”。
        self.load_private_limit = 200
        self.load_group_limit = 100
        self._ensure_database()
        self._conn = self._connect()
        self._init_schema()

    # ---- 连接与执行 ----
    def _connect(self, with_db=True):
        return self._pymysql.connect(
            host=self.cfg["host"], port=self.cfg["port"],
            user=self.cfg["user"], password=self.cfg["password"],
            database=self.cfg["database"] if with_db else None,
            charset=self.cfg["charset"], autocommit=True,
            connect_timeout=10, cursorclass=self._pymysql.cursors.DictCursor)

    def _ensure_database(self):
        conn = self._connect(with_db=False)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{self.cfg['database']}` "
                    f"CHARACTER SET {self.cfg['charset']} COLLATE utf8mb4_unicode_ci")
        finally:
            conn.close()

    def _init_schema(self):
        # MySQL 5.5 兼容：不用 JSON 类型（5.7+ 才有），payload/members 用 LONGTEXT；
        # 二级索引用前缀长度（191*4=764<767）规避 innodb 767 字节上限。
        stmts = [
            """CREATE TABLE IF NOT EXISTS users (
                 name VARCHAR(64) NOT NULL,
                 PRIMARY KEY (name)
               ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
            """CREATE TABLE IF NOT EXISTS groups_t (
                 name VARCHAR(64) NOT NULL,
                 members LONGTEXT,
                 PRIMARY KEY (name)
               ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
            """CREATE TABLE IF NOT EXISTS group_messages (
                 id BIGINT NOT NULL,
                 grp VARCHAR(64) NOT NULL,
                 ts DOUBLE,
                 payload LONGTEXT,
                 PRIMARY KEY (id),
                 KEY idx_grp (grp)
               ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
            """CREATE TABLE IF NOT EXISTS private_messages (
                 id BIGINT NOT NULL,
                 pair_key VARCHAR(255) NOT NULL,
                 ts DOUBLE,
                 payload LONGTEXT,
                 PRIMARY KEY (id),
                 KEY idx_pair (pair_key(191))
               ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
            """CREATE TABLE IF NOT EXISTS offline_messages (
                 seq BIGINT AUTO_INCREMENT PRIMARY KEY,
                 user VARCHAR(64) NOT NULL,
                 msg_id BIGINT,
                 payload LONGTEXT,
                 KEY idx_user (user)
               ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        ]
        with self.lock:
            cur = self._cursor()
            try:
                for s in stmts:
                    cur.execute(s)
            finally:
                cur.close()

    def _cursor(self):
        self._conn.ping(reconnect=True)
        return self._conn.cursor()

    def _run(self, sql, params=(), fetch=False, executemany=False):
        """统一执行入口：持久连接 + 断线重试一次；非查询返回受影响行数。"""
        with self.lock:
            last = None
            for attempt in range(2):
                try:
                    cur = self._cursor()
                    try:
                        if executemany:
                            cur.executemany(sql, params)
                            return cur.rowcount
                        cur.execute(sql, params)
                        return cur.fetchall() if fetch else cur.rowcount
                    finally:
                        cur.close()
                except (self._pymysql.err.OperationalError,
                        self._pymysql.err.InterfaceError) as e:
                    last = e
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = self._connect()
            raise last

    @staticmethod
    def _load(s):
        if s is None:
            return {}
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("utf-8", "replace")
        try:
            return json.loads(s)
        except Exception:
            return {}

    @staticmethod
    def _dump(obj):
        return json.dumps(obj, ensure_ascii=False)

    # ---- 群成员 ----
    def get_groups(self):
        rows = self._run("SELECT name, members FROM groups_t", fetch=True) or []
        out = {}
        for r in rows:
            try:
                out[r["name"]] = json.loads(r["members"]) if r["members"] else []
            except Exception:
                out[r["name"]] = []
        return out

    def set_group(self, name, members):
        self._run(
            "INSERT INTO groups_t (name, members) VALUES (%s,%s) "
            "ON DUPLICATE KEY UPDATE members=VALUES(members)",
            (name, self._dump(list(members))))

    # ---- 群历史（全量留存） ----
    def _recent(self, table, col, key, limit):
        rows = self._run(
            f"SELECT payload FROM {table} WHERE {col}=%s ORDER BY id DESC LIMIT %s",
            (key, limit), fetch=True) or []
        return [self._load(r["payload"]) for r in reversed(rows)]

    def get_all_history(self):
        rows = self._run("SELECT DISTINCT grp FROM group_messages", fetch=True) or []
        return {r["grp"]: self._recent("group_messages", "grp", r["grp"],
                                       self.load_group_limit)
                for r in rows}

    def append_history(self, name, msg, limit=None):
        # limit 忽略：数据库中保留全部历史
        self._run(
            "INSERT INTO group_messages (id, grp, ts, payload) "
            "VALUES (%s,%s,%s,%s)",
            (msg.get("id"), name, msg.get("ts") or 0, self._dump(msg)))

    # ---- 私聊历史（全量留存） ----
    def get_all_private(self):
        rows = self._run("SELECT DISTINCT pair_key FROM private_messages",
                         fetch=True) or []
        return {r["pair_key"]: self.get_private(r["pair_key"]) for r in rows}

    def append_private(self, key, msg, limit=None):
        self._run(
            "INSERT INTO private_messages (id, pair_key, ts, payload) "
            "VALUES (%s,%s,%s,%s)",
            (msg.get("id"), key, msg.get("ts") or 0, self._dump(msg)))

    def get_private(self, key):
        return self._recent("private_messages", "pair_key", key,
                            self.load_private_limit)

    # ---- 离线消息箱 ----
    def get_offline_all(self):
        rows = self._run(
            "SELECT user, payload FROM offline_messages ORDER BY seq ASC",
            fetch=True) or []
        out = {}
        for r in rows:
            out.setdefault(r["user"], []).append(self._load(r["payload"]))
        return out

    def set_offline(self, user, messages):
        self._run("DELETE FROM offline_messages WHERE user=%s", (user,))
        msgs = list(messages)
        if msgs:
            self._run(
                "INSERT INTO offline_messages (user, msg_id, payload) "
                "VALUES (%s,%s,%s)",
                [(user, m.get("id"), self._dump(m)) for m in msgs],
                executemany=True)

    # ---- 撤回（按 id 覆盖 payload） ----
    def replace_history(self, name, msg):
        n = self._run(
            "UPDATE group_messages SET payload=%s WHERE id=%s AND grp=%s",
            (self._dump(msg), msg.get("id"), name))
        return (n or 0) > 0

    def replace_private(self, key, msg):
        n = self._run(
            "UPDATE private_messages SET payload=%s WHERE id=%s AND pair_key=%s",
            (self._dump(msg), msg.get("id"), key))
        return (n or 0) > 0

    # ---- 用户名 ----
    def get_users(self):
        rows = self._run("SELECT name FROM users", fetch=True) or []
        return [r["name"] for r in rows]

    def add_user(self, name):
        self._run("INSERT IGNORE INTO users (name) VALUES (%s)", (name,))


def create_storage():
    """工厂：根据环境变量选择存储实现（默认 JSON）。"""
    kind = os.environ.get("MOYU_STORAGE", "json").lower()
    if kind == "mysql":
        return MysqlStorage()
    return JsonStorage()
