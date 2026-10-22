"""MoyuChat Tkinter 客户端（参考 Telegram 界面风格）。

界面与功能：
  - 用户名登录 TCP 服务器
  - 左侧会话列表：头像(首字母色块)、最后一条消息预览、时间、未读红点、搜索
  - 右侧聊天气泡：己方右侧蓝色气泡 / 对方左侧白色气泡，含时间与单双勾回执
  - 顶部会话头显示对方“在线/离线”状态
  - 私聊、群聊、群历史同步、消息回执（✓ 已发送 / ✓✓ 已读）
  - 仅使用 Python 标准库（tkinter / socket / threading / queue）

运行前请先启动 server.py。
"""

import tkinter as tk
from tkinter import messagebox, simpledialog
import os
import socket
import threading
import queue
import time

import discover

from common import (send_obj, recv_obj, HOST, PORT, MAX_NAME, MAX_TEXT,
                    RECALL_WINDOW, PING_INTERVAL, PING_TIMEOUT, valid_name,
                    lan_ip)

# 现代风格设计 token（参考 TDesign / Telegram 浅色主题，统一配色与层级）
C_BLUE = "#0052D9"            # 主品牌色：按钮 / 强调 / 已读勾
C_BG = "#ffffff"             # 面板底色（侧栏 / 头部 / 输入区）
C_CHAT_BG = "#f5f6f8"        # 聊天区背景
C_SENT = "#eaedf3"           # 己方气泡（浅灰蓝，更克制）
C_RECV = "#ffffff"           # 对方气泡（白）
C_PREVIEW = "#8a8f99"        # 次要文本：预览 / 时间戳
C_SELECT = "#e8f0fe"         # 选中会话行底色（带品牌色的浅蓝）
HOVER_BG = "#f2f3f5"         # 会话行悬停底色
BORDER = "#e7e9ec"           # 分隔线 / 输入框描边
BADGE_BG = "#f5483b"         # 未读红点（圆点样式）
SYS_TEXT = "#d54941"         # 系统消息红字
RECALL_TEXT = "#9aa0a6"      # 撤回占位文字
TEXT_1 = "#1a1a1a"           # 主文本（名称 / 气泡正文）

AVATAR_COLORS = ["#5b8def", "#37c091", "#f2a23c", "#9b7bf0",
                 "#ef6f9e", "#46c0d4", "#e8743b", "#4aa3df"]

# 每个会话本地最多保留的消息条数：聊天窗口每帧都会重建气泡控件，
# 不设上限的话长时间聊天会越用越卡（内存与渲染开销双增长）。
BUFFER_LIMIT = 500
# 聊天区一次渲染多少条（更早的消息由顶部按钮按需展开）
VISIBLE_LIMIT = 100


def avatar_color(name):
    h = 0
    for c in (name or ""):
        h = (h * 31 + ord(c)) & 0xffffffff
    return AVATAR_COLORS[h % len(AVATAR_COLORS)]


class ChatClient:
    def __init__(self, root):
        self.root = root
        self.sock = None
        self.username = None
        self.queue = queue.Queue()

        self.buffers = {}      # 会话名 -> [消息 dict]
        self.seen_ids = {}     # 会话名 -> set(消息 id)：O(1) 去重（回放/补投会重复投递）
        self.conv_type = {}    # 会话名 -> 'user' / 'group'
        self.conv_order = []   # 会话显示顺序（最近活跃在前）
        self.unread = {}       # 会话名 -> 未读数
        self.presence = {}     # username -> bool（在线状态）
        self.group_members = {}  # group -> [成员]
        self.search_term = ""
        self.active = None
        self.active_type = None
        self.wrap_px = 420       # 气泡文本换行宽度（随窗口宽度自适应）
        self.sidebar_w = 260      # 侧栏宽度（随画布自适应，用于预览换行）
        self.resize_job = None   # 窗口尺寸变化的防抖句柄
        self.vis_count = {}      # 会话名 -> 当前展开的消息条数（“载入更早的消息”）
        self._pending_bubbles = []  # 待补画圆角背景的气泡画布（布局完成后统一处理）
        self._r_conv = None      # 上次完整渲染的会话（增量渲染用）
        self._r_start = -1       # 上次渲染窗口的起始下标
        self._r_entries = []     # 上次渲染的消息对象（按身份比对判断是否“只追加”）
        self._r_day = None       # 上次渲染的最后一条消息所属日期
        self._meta_widgets = {}  # 会话名 -> {消息 id: 时间/勾 标签控件}
        self.discover_groups = []  # “发现群聊”缓存的群组列表
        self.discover_win = None   # “发现群聊”窗口
        self.discover_list = None  # “发现群聊”列表控件
        self.members_win = None    # 群成员弹窗
        self.members_list = None   # 群成员列表控件
        self.members_conv = None   # 成员弹窗对应的群（避免刷错窗口）
        self.kicked = False      # 是否已被“同名登录”顶下线（顶下线后不再自动重连）
        self.state_user = None   # 当前界面里的会话数据属于哪个账号

        self.last_typing = 0
        self.typing_timer = None
        self.pinned = set()    # 置顶会话
        self.typing_conv = None  # 当前正在输入的对端会话（用于列表预览）
        self.typing_from = None  # 正在输入的人（群聊里需要显示是谁）
        self.closing = False   # 是否主动关闭（用于抑制断开误报）
        self.ping_job = None   # 心跳定时器
        self.last_pong = 0     # 最近一次收到 pong 的时间
        self.main_built = False  # 主界面是否已建立（重连时不重建）
        self.reconnecting = False
        # 服务器地址（局域网多人聊天用）：登录时可改，重连沿用；默认本机 IP
        self.srv_host = HOST
        self.srv_port = PORT

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.build_login()
        self.root.after(100, self.poll)

    def center_window(self, w, h):
        """把主窗口居中显示（略偏上，视觉更自然）。"""
        try:
            self.root.update_idletasks()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 3)
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            self.root.geometry(f"{w}x{h}")

    def enable_keepalive(self, sock):
        """开启 TCP keepalive，帮助及时发现断线。"""
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass

    def try_send(self, obj):
        """安全发送：socket 断开时触发自动重连，而不是抛异常中断回调。

        返回 True 表示已发出，False 表示发送失败（调用方可据此保留草稿）。
        """
        if self.sock is None:
            self.on_conn_lost()
            return False
        try:
            send_obj(self.sock, obj)
            return True
        except Exception:
            self.on_conn_lost()
            return False

    def on_conn_lost(self):
        """连接丢失：更新状态并尝试自动重连（仿 Telegram 断线重连）。"""
        if self.closing or self.kicked:  # 主动关闭 / 已被顶下线：不再重连
            return
        self.sock = None
        if hasattr(self, "hs"):
            self.hs["text"] = "连接已断开"
            self.hs["fg"] = "#b00020"
        self.start_reconnect()

    def start_heartbeat(self):
        """启动心跳：定期 ping，并对长时间没有 pong 的连接主动重连。

        TCP 的 keepalive 默认要两小时才发现“半开”连接，期间对端会一直显示在线、
        消息也发不出去；应用层心跳能在几十秒内发现并自愈。
        """
        if self.ping_job is not None:
            try:
                self.root.after_cancel(self.ping_job)
            except Exception:
                pass
        self.ping_job = self.root.after(PING_INTERVAL * 1000, self.ping_tick)

    def ping_tick(self):
        self.ping_job = self.root.after(PING_INTERVAL * 1000, self.ping_tick)
        if self.closing or self.kicked or self.sock is None:
            return
        if self.last_pong and time.time() - self.last_pong > PING_TIMEOUT:
            # 长时间收不到 pong：认为链路已死，主动断开触发重连
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            self.on_conn_lost()
            return
        self.try_send({"type": "ping", "ts": time.time()})

    def start_reconnect(self):
        if self.closing or self.reconnecting or self.kicked \
                or not self.username:
            return
        self.reconnecting = True
        self.reconnect_tries = 0
        self.root.after(500, self.reconnect_step)

    def on_kicked(self, reason=None):
        """账号在别处登录被顶下线：停止自动重连，回到登录界面。

        如果这里继续自动重连，就会把对方的连接再踢掉，双方无限互踢。
        """
        self.kicked = True
        self.reconnecting = False
        self.sock = None
        if self.main_built:
            messagebox.showwarning(
                "已下线", reason or "该账号已在其他窗口登录，本窗口已下线")
            self.username = ""
            self.main_built = False
            self.build_login()
        else:
            messagebox.showwarning("登录失败", reason or "该账号已在其他窗口登录")

    def reconnect_step(self):
        if self.closing or self.kicked:
            return
        self.reconnect_tries += 1
        if hasattr(self, "hs"):
            self.hs["text"] = f"重连中…({self.reconnect_tries})"
            self.hs["fg"] = "#f0a000"
        try:
            sock = socket.create_connection(
                (getattr(self, "srv_host", HOST), getattr(self, "srv_port", PORT)),
                timeout=5)
            sock.settimeout(None)
            self.enable_keepalive(sock)
            old, self.sock = self.sock, sock
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            self.closing = False
            send_obj(sock, {"type": "login", "username": self.username})
            threading.Thread(target=self.recv_loop, args=(sock,),
                             daemon=True).start()
            self.reconnecting = False  # 已重新登录，等待 login_ok
        except Exception:
            if self.reconnect_tries >= 5:  # 多次失败后放弃并提示
                self.reconnecting = False
                messagebox.showerror("断开连接",
                                     "与服务器的连接已断开，且自动重连失败。\n"
                                     "请重新启动客户端登录")
                return
            self.root.after(2000, self.reconnect_step)

    # ---------------- 登录界面 ----------------
    def build_login(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.root.title("MoyuChat - 登录")
        self.center_window(360, 260)
        self.root.configure(bg=C_BG)
        f = tk.Frame(self.root, padx=24, pady=24, bg=C_BG)
        f.pack(expand=True)
        tk.Label(f, text="MoyuChat", font=("Microsoft YaHei", 18, "bold"),
                 fg=C_BLUE, bg=C_BG).grid(row=0, column=0, columnspan=2,
                                          pady=(0, 12))
        # 服务器地址：局域网多人聊天时填“服务器那台机器的 IP”（默认填本机 IP）
        tk.Label(f, text="服务器", font=("Microsoft YaHei", 11),
                 bg=C_BG).grid(row=1, column=0, pady=6, sticky="e")
        self.host_var = tk.StringVar(value=lan_ip())
        tk.Entry(f, textvariable=self.host_var, width=18,
                 font=("Microsoft YaHei", 11)).grid(row=1, column=1, pady=6)
        tk.Label(f, text="端口", font=("Microsoft YaHei", 11),
                 bg=C_BG).grid(row=2, column=0, pady=6, sticky="e")
        self.port_var = tk.StringVar(value=str(PORT))
        tk.Entry(f, textvariable=self.port_var, width=18,
                 font=("Microsoft YaHei", 11)).grid(row=2, column=1, pady=6)
        tk.Label(f, text="用户名", font=("Microsoft YaHei", 11),
                 bg=C_BG).grid(row=3, column=0, pady=6, sticky="e")
        self.name_var = tk.StringVar()
        entry = tk.Entry(f, textvariable=self.name_var, width=18,
                         font=("Microsoft YaHei", 11))
        entry.grid(row=3, column=1, pady=6)
        entry.bind("<Return>", lambda e: self.do_connect())  # 回车即登录
        entry.focus_set()
        tk.Button(f, text="登录", command=self.do_connect, width=18,
                  bg=C_BLUE, fg="white",
                  font=("Microsoft YaHei", 11)).grid(
            row=4, column=0, columnspan=2, pady=(10, 6))
        # 自动发现：UDP 广播扫描局域网服务器，免手输 IP（网页端无法用，仅桌面端）
        self.disc_btn = tk.Button(f, text="🔍 自动发现服务器", width=18,
                                  command=self.auto_discover, relief="flat",
                                  bg=C_BG, fg=C_BLUE, cursor="hand2",
                                  activebackground="#eef3ff",
                                  font=("Microsoft YaHei", 10))
        self.disc_btn.grid(row=5, column=0, columnspan=2, pady=(0, 4))
        # 打开登录界面即自动扫一次，静默预填服务器地址
        self.root.after(200, lambda: self.auto_discover(silent=True))

    def do_connect(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("错误", "请输入用户名")
            return
        if not valid_name(name):  # 与服务端一致的前置校验，避免白跑一次连接
            messagebox.showerror("用户名不合法",
                                 f"用户名需为 1~{MAX_NAME} 个可见字符")
            return
        # 读取登录界面填写的服务器地址（断线重连也沿用同一地址）
        host = (self.host_var.get().strip()
                if getattr(self, "host_var", None) else HOST) or HOST
        port_text = (self.port_var.get().strip()
                     if getattr(self, "port_var", None) else str(PORT)) or str(PORT)
        try:
            port = int(port_text)
        except ValueError:
            messagebox.showerror("端口不合法", "端口必须是数字")
            return
        self.srv_host, self.srv_port = host, port
        if self.sock is not None:  # 释放上一次未成功的连接
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        try:
            self.sock = socket.create_connection((self.srv_host, self.srv_port),
                                                 timeout=5)
            self.sock.settimeout(None)  # 连接成功后改回阻塞，避免空闲超时
            self.enable_keepalive(self.sock)
        except Exception as e:
            messagebox.showerror(
                "连接失败",
                f"无法连接 {self.srv_host}:{self.srv_port}\n{e}\n\n"
                "局域网多人聊天请确认：\n"
                "1) 已在服务器机器上启动 server.py；\n"
                "2) 此处填的是服务器机器的局域网 IP；\n"
                "3) 防火墙已允许该端口。")
            return
        self.username = name
        self.closing = False
        self.kicked = False   # 重新登录：清除“被顶下线”状态，恢复自动重连能力
        send_obj(self.sock, {"type": "login", "username": name})
        threading.Thread(target=self.recv_loop, args=(self.sock,),
                         daemon=True).start()

    def _ui_alive(self):
        """界面是否仍存在（后台线程回调前检查，避免root销毁后操作Tk报错）。"""
        try:
            return bool(self.root.winfo_exists())
        except Exception:
            return False

    def auto_discover(self, silent=False):
        """UDP 广播扫描局域网服务器（后台线程，避免卡界面）。

        silent=True 用于打开登录界面时的自动扫描：只静默预填，不弹窗打扰。
        """
        try:
            self.disc_btn.configure(state="disabled", text="扫描中…")
        except Exception:
            pass

        def work():
            servers = discover.discover_sync(1.5)

            def apply():
                if self._ui_alive():
                    self._apply_discovery(servers, silent)

            try:
                self.root.after(0, apply)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _apply_discovery(self, servers, silent=False):
        try:
            self.disc_btn.configure(state="normal", text="🔍 自动发现服务器")
        except Exception:
            pass
        if not servers:
            if not silent:
                messagebox.showinfo(
                    "自动发现", "未在局域网发现服务器。\n"
                    "请确认服务器已启动（python server.py），或手动填写 IP。")
            return
        srv = servers[0]
        self.host_var.set(srv["ip"])
        self.port_var.set(str(srv.get("tcp_port") or PORT))
        if not silent:
            extra = "" if len(servers) == 1 else \
                f"\n（发现 {len(servers)} 台，已填第一台）"
            messagebox.showinfo(
                "自动发现",
                f"已找到服务器「{srv.get('name', 'MoyuChat')}」"
                f"\n地址 {srv['ip']}:{srv.get('tcp_port')}{extra}")

    def notify_new_message(self):
        """新消息提醒：窗口失焦时闪烁任务栏图标（Windows），标题同时显示未读数。

        不做强制弹窗/抢焦点，避免打扰正在输入的用户（Telegram 同理）。
        """
        try:
            if self.root.focus_displayof() is not None:  # 窗口正被使用
                return
        except Exception:
            pass
        try:
            if os.name == "nt":  # Windows：闪烁任务栏图标
                import ctypes
                ctypes.windll.user32.FlashWindow(self.root.winfo_id(), True)
            else:                # 其他平台：退化为系统提示音
                self.root.bell()
        except Exception:
            pass

    def reset_chat_state(self):
        """清空所有会话数据（换账号登录时调用，避免泄露上一个账号的记录）。"""
        self.buffers.clear()
        self.seen_ids.clear()
        self.vis_count.clear()
        self._meta_widgets.clear()
        self._r_conv, self._r_start, self._r_entries, self._r_day = \
            None, -1, [], None
        self.conv_type.clear()
        self.conv_order.clear()
        self.unread.clear()
        self.pinned.clear()
        self.group_members.clear()
        self.presence = {self.username: True} if self.username else {}
        self.active = None
        self.active_type = "user"
        self.typing_conv = None
        self.typing_from = None
        self.discover_groups = []
        self.search_term = ""

    def on_close(self):
        """关闭窗口：主动断开，避免 recv 线程误报断开。"""
        self.closing = True
        if self.ping_job is not None:
            try:
                self.root.after_cancel(self.ping_job)
            except Exception:
                pass
            self.ping_job = None
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self.root.destroy()

    def recv_loop(self, sock=None):
        sock = sock or self.sock
        try:
            while not self.closing and sock is not None:
                msg = recv_obj(sock)  # 固定使用本线程的 socket，避免重连后串读
                if msg is None:
                    break
                self.queue.put(msg)
        except Exception:
            pass
        # 仅当仍在使用同一个 socket 且非主动关闭时，才上报断开（重连后旧线程静默退出）
        if not self.closing and sock is self.sock:
            self.queue.put({"type": "disconnected"})

    # ---------------- 主界面 ----------------
    def build_main(self):
        if self.main_built:  # 重连后不重建界面（保留聊天记录与选择）
            return
        if self.state_user is not None and self.state_user != self.username:
            self.reset_chat_state()  # 换了账号：绝不能把上一个人的聊天记录带过去
        self.state_user = self.username
        self.main_built = True
        for w in self.root.winfo_children():
            w.destroy()
        self.root.title(f"MoyuChat - {self.username}")
        self.center_window(940, 640)
        self.root.configure(bg=C_BG)
        self.root.columnconfigure(0, weight=0)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        # ===== 左侧会话栏 =====
        left = tk.Frame(self.root, width=300, bg=C_BG,
                        highlightbackground="#e6ebee", highlightthickness=1)
        left.grid(row=0, column=0, sticky="ns")
        left.grid_propagate(False)
        tk.Label(left, text="MoyuChat", font=("Microsoft YaHei", 15, "bold"),
                 fg=C_BLUE, bg=C_BG).pack(anchor="w", padx=16, pady=(14, 6))
        self.search = tk.Entry(left, font=("Microsoft YaHei", 10),
                               relief="flat", bg=C_BG, fg=C_PREVIEW,
                               highlightbackground=BORDER, highlightthickness=1,
                               insertbackground=TEXT_1)
        self.search.pack(fill="x", padx=12, pady=(0, 8), ipady=4)
        self.search.insert(0, "搜索")
        self.search.bind("<FocusIn>",
                         lambda e: self.search.delete(0, tk.END)
                         if self.search.get() == "搜索" else None)
        self.search.bind("<KeyRelease>", lambda e: self.on_search())
        self.search.bind("<FocusOut>",
                         lambda e: self.search.insert(0, "搜索")
                         if not self.search.get() else None)
        # 回车 = 跨服务器发现：搜索该用户名所在服务器（本地没有也会问对端）
        self.search.bind("<Return>",
                         lambda e: self.on_search_query())

        self.conv_canvas = tk.Canvas(left, bg=C_BG, highlightthickness=0)
        self.conv_scroll = tk.Scrollbar(left, orient="vertical",
                                        command=self.conv_canvas.yview)
        self.conv_canvas.configure(yscrollcommand=self.conv_scroll.set)
        self.conv_canvas.pack(side="left", fill="both", expand=True)
        self.conv_scroll.pack(side="right", fill="y")
        self.conv_inner = tk.Frame(self.conv_canvas, bg=C_BG)
        self.conv_win = self.conv_canvas.create_window(
            (0, 0), window=self.conv_inner, anchor="nw")
        self.conv_inner.bind(
            "<Configure>", lambda e: self.conv_canvas.configure(
                scrollregion=self.conv_canvas.bbox("all")))
        # 内容宽度始终等于可视宽度（否则右对齐的未读红点贴不到右边缘）
        def _on_side(e):
            self.conv_canvas.itemconfigure(self.conv_win, width=e.width)
            self.sidebar_w = e.width
        self.conv_canvas.bind("<Configure>", _on_side)

        # ===== 右侧聊天区 =====
        right = tk.Frame(self.root, bg=C_CHAT_BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(2, weight=1)  # 聊天区行可扩展
        right.columnconfigure(0, weight=1)

        self.header = tk.Frame(right, bg=C_BG, height=58)
        self.header.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.header.grid_propagate(False)
        self.ha = tk.Canvas(self.header, width=36, height=36, bg=C_BG,
                            highlightthickness=0)
        self.ha.pack(side="left", padx=14)
        self.ht = tk.Label(self.header, font=("Microsoft YaHei", 13, "bold"),
                           bg=C_BG)
        self.ht.pack(side="left", padx=(2, 0))
        self.hs = tk.Label(self.header, font=("Microsoft YaHei", 10),
                           fg=C_PREVIEW, bg=C_BG)
        self.hs.pack(side="left", padx=(8, 0))
        for lb in (self.ht, self.hs):
            lb.bind("<Button-1>", self.on_header_click)
            lb.configure(cursor="hand2")  # 仿 Telegram：点头部可看群成员
        for txt, cmd in (("新建群聊", self.new_group),
                         ("发现群聊", self.open_discover),
                         ("刷新", self.refresh_lists),
                         ("邀请成员", self.invite_member)):
            tk.Button(self.header, text=txt, command=cmd, bg=C_BG, fg=C_BLUE,
                      relief="flat", bd=0, cursor="hand2",
                      activebackground=HOVER_BG, activeforeground=C_BLUE,
                      font=("Microsoft YaHei", 10)).pack(side="right", padx=8)

        self.chat_canvas = tk.Canvas(right, bg=C_CHAT_BG, highlightthickness=0)
        self.chat_scroll = tk.Scrollbar(right, orient="vertical",
                                        command=self.chat_canvas.yview)
        self.chat_canvas.configure(yscrollcommand=self.chat_scroll.set)
        self.chat_canvas.grid(row=2, column=0, sticky="nsew")
        self.chat_scroll.grid(row=2, column=1, sticky="ns")
        self.chat_inner = tk.Frame(self.chat_canvas, bg=C_CHAT_BG)
        self.chat_win = self.chat_canvas.create_window(
            (0, 0), window=self.chat_inner, anchor="nw")
        self.chat_inner.bind(
            "<Configure>", lambda e: self.chat_canvas.configure(
                scrollregion=self.chat_canvas.bbox("all")))
        # 内容宽度随画布，气泡才能正确左右贴边；窗口变化时重排换行
        self.chat_canvas.bind("<Configure>", self.on_chat_resize)

        # 会话头与聊天区之间的细分隔线
        sep = tk.Frame(right, height=1, bg=BORDER)
        sep.grid(row=1, column=0, columnspan=2, sticky="ew")

        bottom = tk.Frame(right, bg=C_BG)
        bottom.grid(row=3, column=0, columnspan=2, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        # Telegram 风格：圆角描边的输入框（用高亮描边模拟，内容随输入增高）
        field = tk.Frame(bottom, bg=C_BG, highlightbackground=BORDER,
                         highlightthickness=1)
        field.grid(row=0, column=0, padx=14, pady=8, sticky="ew")
        field.columnconfigure(0, weight=1)
        self.input = tk.Text(field, font=("Microsoft YaHei", 11), relief="flat",
                             height=1, wrap="word", bg=C_BG, bd=0,
                             highlightthickness=0, fg="#9a9a9a")
        self.input.grid(row=0, column=0, padx=10, pady=6, sticky="ew")
        self.input.insert("1.0", "消息")  # 占位提示
        self.input.bind("<Return>", self.on_input_return)
        self.input.bind("<KeyPress>", self.on_key_typing)
        self.input.bind("<KeyRelease>", self.adjust_input_height)
        self.input.bind("<FocusIn>", self.on_input_focus_in)
        self.input.bind("<FocusOut>", self.on_input_focus_out)
        # 圆形蓝色发送按钮（仿 Telegram，画布绘制箭头避免字体缺失）
        self.send_btn = tk.Canvas(bottom, width=34, height=34, bg=C_BG,
                                  highlightthickness=0)
        self.send_btn.create_oval(2, 2, 32, 32, fill=C_BLUE, outline=C_BLUE)
        self.send_btn.create_polygon(12, 11, 12, 23, 25, 17, fill="white")
        self.send_btn.grid(row=0, column=1, padx=8, pady=4)
        self.send_btn.bind("<Button-1>", lambda e: self.send_msg())

        self.render_sidebar()
        self.update_header()

    # ---------------- 侧边栏 ----------------
    def on_search(self):
        self.search_term = self.search.get()
        if self.search_term == "搜索":
            self.search_term = ""
        self.render_sidebar()

    def on_search_query(self):
        """回车搜索：向服务器查询用户名（含联邦对端），结果用 search_result 回传。"""
        name = (self.search.get() or "").strip()
        if not name or name == "搜索":
            return
        if not valid_name(name):
            return
        self.try_send({"type": "query_user", "user": name})

    def on_search_result(self, users):
        """展示发现结果：本地用户直接开聊；异地用户（remote）由服务器路由转发。"""
        users = users or []
        if not users:
            messagebox.showinfo("跨服务器发现", "未找到该用户（本服务器及已连接的对等服务器）")
            return

        def open_chat(uname):
            self.ensure_conv(uname, "user")
            self.select_conv(uname)
            dlg.destroy()

        dlg = tk.Toplevel(self.root)
        dlg.title("发现结果")
        dlg.configure(bg=C_BG)
        tk.Label(dlg, text=f"找到 {len(users)} 个匹配：",
                 bg=C_BG, fg=C_TEXT, font=("Microsoft YaHei", 11)).pack(
            padx=16, pady=(12, 6))
        for u in users:
            tag = "本服务器" if not u.get("remote") else \
                f"异地（服务器 {u.get('server', '?')}）"
            row = tk.Frame(dlg, bg=C_BG)
            row.pack(fill="x", padx=12, pady=3)
            tk.Label(row, text=f"{u['name']}  ·  {tag}", bg=C_BG, fg=C_TEXT,
                     font=("Microsoft YaHei", 10)).pack(side="left")
            tk.Button(row, text="聊天", command=lambda n=u["name"]: open_chat(n),
                      bg=C_BLUE, fg="white", relief="flat",
                      font=("Microsoft YaHei", 9)).pack(side="right")
        tk.Button(dlg, text="关闭", command=dlg.destroy, relief="flat",
                  bg="#eef3ff", fg=C_BLUE, font=("Microsoft YaHei", 10)).pack(
            pady=(6, 12))

    def ensure_conv(self, name, ctype):
        self.conv_type[name] = ctype
        if name not in self.conv_order:
            self.conv_order.append(name)

    def move_to_front(self, name):
        if name in self.conv_order:
            self.conv_order.remove(name)
        self.conv_order.insert(0, name)

    def conv_matches(self, name, term):
        """搜索匹配：会话名或历史消息内容命中即可（仿 Telegram 全局搜索）。"""
        if term in name.lower():
            return True
        for e in self.buffers.get(name, []):
            if term in (e.get("text") or "").lower():
                return True
        return False

    def render_sidebar(self):
        if not self.main_built:  # 尚无主界面（登录中/已被顶下线）
            return
        for w in self.conv_inner.winfo_children():
            w.destroy()
        term = (self.search_term or "").strip().lower()
        # 置顶会话始终排在前面（保持各自相对顺序）
        order = ([n for n in self.conv_order if n in self.pinned]
                 + [n for n in self.conv_order if n not in self.pinned])
        shown = 0
        for name in order:
            if term and not self.conv_matches(name, term):
                continue
            self.make_conv_row(name)
            shown += 1
        if shown == 0:  # 空列表/无匹配时的友好提示
            tk.Label(self.conv_inner,
                     text="无匹配会话" if term else "还没有会话，去聊天吧",
                     font=("Microsoft YaHei", 10), fg=C_PREVIEW, bg=C_BG
                     ).pack(pady=24)
        self.conv_canvas.update_idletasks()
        self.conv_canvas.configure(
            scrollregion=self.conv_canvas.bbox("all"))
        self.update_title()

    def update_title(self):
        if not self.main_built:
            return
        total = sum(self.unread.values())
        base = f"MoyuChat - {self.username}"
        self.root.title(base + (f" ({total})" if total else ""))

    def draw_avatar(self, c, name, size=36, online=None):
        """在画布 c 上绘制圆形头像；online 不为 None 时显示在线状态点。"""
        for it in c.find_all():
            c.delete(it)
        col = avatar_color(name)
        pad = 2
        c.create_oval(pad, pad, size - pad, size - pad, fill=col, outline=col)
        # 名字为空时用 "?"，避免 IndexError（服务端已校验，此处兜底）
        initial = (name or "?").strip()[:1].upper() or "?"
        c.create_text(size / 2, size / 2, text=initial, fill="white",
                      font=("Microsoft YaHei", max(8, int(size / 3)), "bold"))
        if online is not None:  # 在线(绿)/离线(灰) 小圆点
            r = 4
            cx, cy = size - 6, size - 6
            c.create_oval(cx - r, cy - r, cx + r, cy + r,
                          fill=("#3aaf5c" if online else "#9a9a9a"),
                          outline="white", width=2)

    def make_avatar(self, parent, name, size=36, online=None):
        """创建圆形头像画布（首字母色块，仿 Telegram）。"""
        c = tk.Canvas(parent, width=size, height=size, bg=parent["bg"],
                      highlightthickness=0)
        self.draw_avatar(c, name, size, online)
        return c

    def make_badge(self, parent, count):
        """未读小红点（圆形 + 数字），仿 Telegram 会话列表右侧的未读标记。"""
        c = tk.Canvas(parent, width=18, height=18,
                      bg=parent["bg"], highlightthickness=0)
        self.draw_badge(c, count)
        return c

    def draw_badge(self, c, count):
        c.delete("all")
        if not count:
            return
        text = "99+" if count > 99 else str(count)
        r = 9 if len(text) <= 2 else 11
        c.create_oval(9 - r, 9 - r, 9 + r, 9 + r, fill=BADGE_BG,
                      outline=BADGE_BG)
        c.create_text(9, 9, text=text, fill="white",
                      font=("Microsoft YaHei", 9, "bold"))

    def make_conv_row(self, name):
        active = (name == self.active)
        base = C_SELECT if active else C_BG
        row = tk.Frame(self.conv_inner, bg=base)
        row.pack(fill="x", padx=8, pady=3)
        online = self.presence.get(name, False) \
            if self.conv_type.get(name) == "user" else None
        av = self.make_avatar(row, name, size=44, online=online)
        av.pack(side="left", padx=(6, 10), pady=8)
        mid = tk.Frame(row, bg=base)
        mid.pack(side="left", fill="x", expand=True, pady=8)
        name_line = tk.Frame(mid, bg=base)
        name_line.pack(fill="x")
        tk.Label(name_line, text=name, font=("Microsoft YaHei", 11, "bold"),
                 bg=base, fg=TEXT_1).pack(side="left")
        if name in self.pinned:  # 置顶标记
            tk.Label(name_line, text="置顶", font=("Microsoft YaHei", 8),
                     fg=C_BLUE, bg=base).pack(side="left", padx=(4, 0))
        tk.Label(name_line, text=self.last_time(name),
                 font=("Microsoft YaHei", 9), fg=C_PREVIEW,
                 bg=base).pack(side="right")
        typing = (self.typing_conv == name)
        tk.Label(mid, text=self.preview_of(name),
                 font=("Microsoft YaHei", 9),
                 fg=C_BLUE if typing else C_PREVIEW, bg=base,
                 anchor="w",
                 wraplength=max(60, self.sidebar_w - 92)).pack(fill="x")
        u = self.unread.get(name, 0)
        badge = self.make_badge(name_line, u)  # 未读小红点
        if u:
            badge.pack(side="right", padx=(6, 0))
        self.bind_tree(row, lambda e, n=name: self.select_conv(n))
        self.bind_tree(row, lambda e, n=name: self.conv_menu(e, n),
                       "<Button-3>")  # 右键菜单（置顶/删除）
        # 悬停高亮（Telegram 风格）；用坐标判断避免子控件间切换时闪烁
        def _within(_e):
            try:
                if not row.winfo_ismapped():
                    return False
                x = row.winfo_pointerx() - row.winfo_rootx()
                y = row.winfo_pointery() - row.winfo_rooty()
                return 0 <= x < row.winfo_width() and 0 <= y < row.winfo_height()
            except Exception:
                return False

        def on_enter(e):
            if not active and _within(e):
                self._set_bg_recursive(row, HOVER_BG)

        def on_leave(e):
            if not active and not _within(e):
                self._set_bg_recursive(row, base)

        self.bind_tree(row, on_enter, "<Enter>")
        self.bind_tree(row, on_leave, "<Leave>")

    def build_conv_menu(self, name):
        """构建会话右键菜单（独立出来便于测试，popup 会阻塞事件循环）。"""
        m = tk.Menu(self.root, tearoff=0)
        if name in self.pinned:
            m.add_command(label="取消置顶",
                          command=lambda: self.toggle_pin(name))
        else:
            m.add_command(label="置顶", command=lambda: self.toggle_pin(name))
        m.add_command(label="删除会话", command=lambda: self.delete_conv(name))
        if self.conv_type.get(name) == "group":
            m.add_separator()
            m.add_command(label="查看成员",
                          command=lambda: self.show_members_of(name))
            m.add_command(label="退出群组",
                          command=lambda: self.leave_group(name))
        return m

    def show_members_of(self, name):
        """从右键菜单查看指定群的成员（会先切换到该群）。"""
        if self.conv_type.get(name) == "group":
            self.select_conv(name)
            self.show_members()

    def conv_menu(self, event, name):
        """右键弹出会话菜单：置顶 / 删除会话 / （群聊）退出群组。"""
        m = self.build_conv_menu(name)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def leave_group(self, name):
        if self.sock is None:
            return
        if not messagebox.askyesno("退出群组", f"确定退出群组 {name}？"):
            return
        self.try_send({"type": "group_leave", "group": name})
        self.delete_conv(name, confirm=False)

    def toggle_pin(self, name):
        if name in self.pinned:
            self.pinned.discard(name)
        else:
            self.pinned.add(name)
        self.render_sidebar()

    def delete_conv(self, name, confirm=True):
        if not isinstance(name, str) or not name:
            return
        if confirm and not messagebox.askyesno("删除会话",
                                               f"确定删除与 {name} 的会话？"):
            return
        self.buffers.pop(name, None)
        self.seen_ids.pop(name, None)
        self.vis_count.pop(name, None)
        self._meta_widgets.pop(name, None)
        self.unread.pop(name, None)
        self.conv_type.pop(name, None)
        self.group_members.pop(name, None)
        self.pinned.discard(name)
        if name in self.conv_order:
            self.conv_order.remove(name)
        if self.active == name:  # 关闭当前会话
            self.active = None
            self.active_type = None
        self.render_sidebar()
        self.render_chat(force_bottom=True, force=True)
        self.update_header()

    def _set_bg_recursive(self, w, color):
        try:
            w.configure(bg=color)
        except Exception:
            pass
        for ch in w.winfo_children():
            self._set_bg_recursive(ch, color)

    def bind_tree(self, widget, func, event="<Button-1>"):
        widget.bind(event, func)
        for c in widget.winfo_children():
            self.bind_tree(c, func, event)

    def one_line(self, text, limit=40):
        """把多行文本压成单行并截断，供列表预览使用（避免撑破会话行）。"""
        flat = " ".join((text or "").split())
        return flat if len(flat) <= limit else flat[:limit] + "…"

    def preview_of(self, name):
        if self.typing_conv == name:  # 仿 Telegram：列表预览显示“正在输入…”
            if self.conv_type.get(name) == "group" and self.typing_from:
                return f"{self.typing_from} 正在输入…"
            return "正在输入…"
        buf = self.buffers.get(name, [])
        if not buf:
            return "暂无消息"
        e = buf[-1]
        if e.get("recalled"):
            return "消息已撤回"
        if e["system"]:
            return "系统消息"
        if e["sender"] == self.username:
            return "你: " + self.one_line(e["text"])
        if self.conv_type.get(name) == "group":  # 群聊预览带发送者昵称
            return e["sender"] + ": " + self.one_line(e["text"])
        return self.one_line(e["text"])

    def last_time(self, name):
        buf = self.buffers.get(name, [])
        if not buf or not buf[-1]["ts"]:
            return ""
        return time.strftime("%H:%M", time.localtime(buf[-1]["ts"]))

    def select_conv(self, name):
        self.active = name
        self.active_type = self.conv_type.get(name, "user")
        self.unread[name] = 0
        self.move_to_front(name)
        self.send_read_receipts(name)  # 打开会话即回执已读（否则对方一直是单勾）
        self.render_sidebar()
        self.render_chat(force_bottom=True, force=True)
        self.update_header()
        self.input.focus_set()

    def update_header(self):
        if not self.main_built:
            return
        for it in self.ha.find_all():  # 清空旧头像
            self.ha.delete(it)
        if not self.active:
            self.ht["text"] = ""
            self.hs["text"] = ""
            return
        name = self.active
        on = self.presence.get(name, False) if self.active_type == "user" else None
        self.draw_avatar(self.ha, name, 36, on)
        self.ht["text"] = name
        if self.sock is None and self.reconnecting:  # 断线重连中优先提示状态
            self.hs["text"] = "连接已断开，正在重连…"
            self.hs["fg"] = "#b00020"
            return
        if self.active_type == "user":
            if self.typing_conv == name:  # 正在输入优先显示
                self.hs["text"] = "正在输入…"
                self.hs["fg"] = C_BLUE
            else:
                self.hs["text"] = "在线" if on else "离线"
                self.hs["fg"] = "#3aaf5c" if on else C_PREVIEW
        elif self.typing_conv == name:
            who = self.typing_from or "成员"
            self.hs["text"] = f"{who} 正在输入…"  # 群聊里要能看出是谁在输入
            self.hs["fg"] = C_BLUE
        else:
            n = len(self.group_members.get(name, []))
            self.hs["text"] = f"群组 · {n} 人" if n else "群组"
            self.hs["fg"] = C_PREVIEW

    def on_typing(self, from_user, conv):
        """在会话头与列表预览显示“对方正在输入…”（仿 Telegram）。"""
        if from_user == self.username or conv is None:
            return
        self.typing_conv = conv  # 列表预览用
        self.typing_from = from_user
        if self.typing_timer:
            self.root.after_cancel(self.typing_timer)
        self.typing_timer = self.root.after(3000, self.clear_typing)
        if conv == self.active:
            self.update_header()  # 群聊显示“某某正在输入…”，私聊显示“正在输入…”
        self.render_sidebar()

    def clear_typing(self):
        if self.typing_timer:
            try:
                self.root.after_cancel(self.typing_timer)
            except Exception:
                pass
        self.typing_conv = None
        self.typing_from = None
        self.typing_timer = None
        self.render_sidebar()
        self.update_header()

    def on_chat_resize(self, event):
        """聊天区尺寸变化：内容宽度跟随画布，并自适应气泡换行宽度。"""
        try:
            self.chat_canvas.itemconfigure(self.chat_win, width=event.width)
        except Exception:
            return
        new_wrap = max(180, min(560, int(event.width * 0.62)))
        if abs(new_wrap - self.wrap_px) <= 40:  # 变化不大则不重排，避免抖动
            return
        self.wrap_px = new_wrap
        if self.resize_job is not None:
            try:
                self.root.after_cancel(self.resize_job)
            except Exception:
                pass
        self.resize_job = self.root.after(140, self.render_chat)

    def day_of(self, entry):
        return time.strftime("%Y-%m-%d", time.localtime(entry["ts"])) \
            if entry["ts"] else None

    def visible_slice(self, name):
        """返回应渲染的 (起始下标, 消息列表)：只画最近 VISIBLE_LIMIT 条。

        聊天区每次都重建气泡控件，若把整段历史都画出来，消息越多单次渲染越慢
        （实测 500 条时每来一条新消息都要 1s 以上）。这里只渲染可见窗口，
        更早的消息由顶部“载入更早的消息”按需展开。
        """
        buf = self.buffers.get(name, [])
        n = self.vis_count.get(name, VISIBLE_LIMIT)
        start = max(0, len(buf) - n)
        return start, buf[start:]

    def render_chat(self, force_bottom=False, force=False):
        if not self.main_built:
            return
        # 记录是否已在底部，避免新消息打断用户阅读历史
        try:
            at_bottom = self.chat_canvas.yview()[1] >= 0.999
        except Exception:
            at_bottom = True
        conv = self.active
        if not conv:
            self._r_conv = None
            self._meta_widgets.clear()
            for w in self.chat_inner.winfo_children():
                w.destroy()
            hint = tk.Label(self.chat_inner, text="选择一个会话开始聊天",
                            font=("Microsoft YaHei", 12), fg=C_PREVIEW,
                            bg=C_CHAT_BG)
            hint.pack(expand=True)
            return
        start, visible = self.visible_slice(conv)
        plan = None if force or self._r_conv != conv \
            else self.diff_render(self._r_entries, visible)
        if plan is not None:
            # 增量快路：只补画新增气泡，并丢掉已经滑出窗口/被裁剪的旧气泡
            keep, newer = plan
            self.trim_bubbles(conv, keep)
            self.append_bubbles(conv, newer, self._r_day)
            self.trim_bubbles(conv, len(visible))
            self.scroll_chat(at_bottom, force_bottom)
            return
        for w in self.chat_inner.winfo_children():
            w.destroy()
        self._meta_widgets[conv] = {}
        self._r_widgets = []
        self._r_entries = []
        if start > 0:  # 还有更早的消息没显示（增量追加时不会重算，故不显示条数）
            tk.Button(self.chat_inner, relief="flat", bd=0, cursor="hand2",
                      font=("Microsoft YaHei", 9), fg=C_BLUE, bg=C_CHAT_BG,
                      activebackground=C_CHAT_BG,
                      text="载入更早的消息",
                      command=lambda n=conv: self.load_earlier(n)
                      ).pack(pady=(8, 2))
        self.append_bubbles(conv, visible, None)
        self.trim_bubbles(conv, len(visible))
        self._r_conv = conv
        self._r_start = start
        self.scroll_chat(at_bottom, force_bottom)

    def diff_render(self, rendered, visible):
        """对比“上次已渲染内容”与“当前可见窗口”，判断是否可增量渲染。

        返回 (可复用的条数, 需要补画的消息)；无法增量时返回 None（整屏重绘）。
        消息只会在尾部新增、在头部被裁剪，因此已渲染内容必然是可见窗口中的
        一段连续区间，且以最后一条消息为锚点即可定位。
        """
        if not rendered:
            return 0, visible
        last = rendered[-1]
        k = None
        for i in range(len(visible) - 1, -1, -1):  # 定位上次最后一条消息的位置
            if visible[i] is last:
                k = i
                break
        if k is None:
            return None                     # 内容整体变了，整屏重绘
        keep = min(len(rendered), k + 1)
        start = k + 1 - keep                # 可复用区间在窗口中的起始下标
        if start > 0:                       # 需要在头部插入更旧的消息 → 整屏重绘
            return None
        for off in range(keep):             # 校验这段内容确实未被改写
            if visible[off] is not rendered[len(rendered) - keep + off]:
                return None
        return keep, visible[keep:]

    def append_bubbles(self, conv, entries, last_day):
        """把 entries 依次画到聊天区尾部；每条消息一个控件，便于增量维护。"""
        for entry in entries:
            day = self.day_of(entry)
            show_day = bool(day) and day != last_day
            row = self.make_bubble(entry, conv, show_day, day)
            self._r_widgets.append(row)
            self._r_entries.append(entry)
            last_day = day or last_day
        self._r_day = last_day
        return last_day

    def trim_bubbles(self, conv, keep):
        """只保留最近 keep 条的气泡控件，防止控件越积越多拖慢渲染。"""
        over = max(0, len(self._r_entries) - keep)
        if not over:
            return
        del self._r_entries[:over]
        for w in self._r_widgets[:over]:
            try:
                w.destroy()
            except tk.TclError:
                pass
        del self._r_widgets[:over]
        widgets = self._meta_widgets.get(conv)
        if widgets:  # 同步清理已被销毁气泡的登记项，避免无谓增长
            alive = {e.get("id") for e in self._r_entries}
            for mid in [k for k in widgets if k not in alive]:
                widgets.pop(mid, None)

    def scroll_chat(self, at_bottom, force_bottom):
        self.chat_canvas.update_idletasks()
        self._finalize_bubbles()       # 布局完成后为气泡补画圆角背景
        self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))
        if force_bottom or at_bottom:  # 切换会话/发送时强制到底；否则仅当原在底部
            self.chat_canvas.yview_moveto(1.0)

    def load_earlier(self, name):
        """展开更早的消息（每次多显示一屏）。"""
        self.vis_count[name] = self.vis_count.get(name, VISIBLE_LIMIT) \
            + VISIBLE_LIMIT
        if name == self.active:
            self.render_chat(force=True)

    def make_date_chip(self, parent, day):
        today = time.strftime("%Y-%m-%d")
        label = "今天" if day == today else day
        chip = tk.Frame(parent, bg=C_CHAT_BG)
        chip.pack(anchor="center", fill="x", pady=(12, 2))
        tk.Label(chip, text=label, bg="#e7e9ec", fg="#5a6573",
                 font=("Microsoft YaHei", 9), padx=12, pady=3).pack()

    @staticmethod
    def _round_rect(c, x, y, w, h, r, **kw):
        """在 Canvas 上画一个圆角矩形（四角用扇形填充，避免尖角），返回末项 id。"""
        c.create_arc(x, y, x + 2 * r, y + 2 * r, start=90, extent=90,
                     style="pieslice", **kw)
        c.create_arc(x + w - 2 * r, y, x + w, y + 2 * r, start=0, extent=90,
                     style="pieslice", **kw)
        c.create_arc(x + w - 2 * r, y + h - 2 * r, x + w, y + h, start=270,
                     extent=90, style="pieslice", **kw)
        c.create_arc(x, y + h - 2 * r, x + 2 * r, y + h, start=180, extent=90,
                     style="pieslice", **kw)
        c.create_rectangle(x + r, y, x + w - r, y + h, **kw)
        return c.create_rectangle(x, y + r, x + w, y + h - r, **kw)

    def make_bubble(self, entry, conv=None, show_day=False, day=None):
        """画一条消息，返回该消息的根控件（一条消息 = 一个控件，便于增量增删）。"""
        conv = conv or self.active
        sent = (entry["sender"] == self.username)
        show_avatar = (self.active_type == "group" and not sent
                       and not entry["system"])
        row = tk.Frame(self.chat_inner, bg=C_CHAT_BG)
        row.pack(fill="x")
        if show_day:  # 日期分隔条（仿 Telegram），放在本条消息所在行的顶部
            self.make_date_chip(row, day)
        outer = tk.Frame(row, bg=C_CHAT_BG)
        outer.pack(anchor="e" if sent else "w", padx=14, pady=4, fill="x")
        if show_avatar:  # 群聊中对方消息显示头像（仿 Telegram）
            av = self.make_avatar(outer, entry["sender"], size=28)
            av.pack(side="left", padx=(0, 6))
        color = C_SENT if sent else C_RECV
        corner = BORDER if not sent else color  # 对方气泡用浅描边，己方同色
        # 圆角气泡：Canvas 画圆角矩形作背景，内容（文字/昵称/时间）以 window 叠在上面
        inner = tk.Canvas(outer, bg=C_CHAT_BG, highlightthickness=0)
        content = tk.Frame(inner, bg=color)
        if show_avatar:  # 群聊中显示发送者彩色昵称
            tk.Label(content, text=entry["sender"], bg=color,
                     fg=avatar_color(entry["sender"]),
                     font=("Microsoft YaHei", 9, "bold"),
                     padx=10, pady=4).pack(anchor="w")
        recalled = entry.get("recalled")
        if recalled:  # 已撤回：显示占位文案（仿 Telegram）
            text, fg = "消息已撤回", RECALL_TEXT
            font = ("Microsoft YaHei", 10, "italic")
        else:
            text = ("⚠ " + entry["text"]) if entry["system"] else entry["text"]
            fg = SYS_TEXT if entry["system"] else TEXT_1
            font = ("Microsoft YaHei", 10)
        wrap = self.wrap_px
        if self.active_type == "group" and not sent and not entry["system"]:
            wrap -= 34  # 群聊对方消息左侧有头像，留出空间
        tk.Label(content, text=text, wraplength=max(120, wrap), justify="left",
                 bg=color, fg=fg, font=font,
                 padx=10, pady=6).pack()
        meta_text, meta_fg = self.meta_of(entry, sent)
        meta = tk.Label(content, text=meta_text, bg=color, fg=meta_fg,
                        font=("Microsoft YaHei", 8), padx=10, pady=4)
        meta.pack(anchor="e")
        inner.create_window((10, 9), window=content, anchor="nw")
        inner.pack(side="right" if sent else "left")
        # 圆角矩形背景要等整个聊天区布局完成（window item 被映射）后才能量出
        # 内容真实尺寸，因此延后到 scroll_chat 里统一绘制（见 _finalize_bubbles）
        inner._pending = (content, color, corner)
        self._pending_bubbles.append(inner)
        if entry.get("id") is not None:
            # 登记“时间/勾”标签，收已读回执时只改这一个控件，无需整屏重绘
            self._meta_widgets.setdefault(conv, {})[entry["id"]] = meta
        # 右键气泡：复制 / 删除消息（本地删除，仿 Telegram 的 delete for me）
        handler = lambda e, en=entry: self.bubble_menu(e, en)
        self.bind_tree(content, handler, "<Button-3>")
        inner.bind("<Button-3>", handler)
        return row

    def _finalize_bubbles(self):
        """为已渲染但还没量过尺寸的气泡画圆角背景（布局完成后才能量尺寸）。"""
        pending = self._pending_bubbles
        self._pending_bubbles = []
        for inner in pending:
            try:
                if not inner.winfo_exists():
                    continue
                content, color, corner = inner._pending
                cw = content.winfo_width() or content.winfo_reqwidth() or 40
                ch = content.winfo_height() or content.winfo_reqheight() or 24
                W, H = cw + 20, ch + 18
                inner.configure(width=W, height=H)
                rid = self._round_rect(inner, 0, 0, W - 1, H - 1, 14,
                                       fill=color, outline=corner)
                inner.tag_lower(rid)   # 圆角背景置于内容之下
            except tk.TclError:
                continue

    def build_bubble_menu(self, entry):
        """构建消息气泡右键菜单（独立出来便于测试，popup 会阻塞事件循环）。"""
        m = tk.Menu(self.root, tearoff=0)
        if not entry.get("recalled"):
            m.add_command(label="复制",
                          command=lambda: self.copy_message(entry))
        if not entry["system"]:
            m.add_command(label="删除消息",
                          command=lambda: self.delete_message(entry))
            if self.can_recall(entry):  # 自己的消息且未超时可撤回
                m.add_separator()
                m.add_command(label="撤回",
                              command=lambda: self.recall_message(entry))
        return m

    def on_recalled(self, mid, conv, from_user=None):
        """收到撤回广播：把本地对应消息标记为已撤回并刷新。

        conv 只是提示：私聊里双方对同一个会话的叫法不同（各自叫对方的名字），
        因此按 id 全局兜底查找，保证收方也能看到“消息已撤回”；
        from_user 用于确认要撤的确实是对方/自己发的那条消息。
        """
        if not isinstance(mid, (int, str)):
            return
        order = [conv] if isinstance(conv, str) and conv in self.buffers else []
        order += [n for n in self.buffers if n not in order]
        changed = []
        for name in order:
            touched = False
            for e in self.buffers.get(name, []):
                if e.get("id") != mid or e.get("recalled"):
                    continue
                if isinstance(from_user, str) and e.get("sender") != from_user:
                    continue  # 广播声称的作者与本地记录不符，不动它
                e["recalled"] = True
                e["text"] = ""
                touched = True
            if touched:
                changed.append(name)
        if not changed:
            return
        if self.active in changed:
            self.render_chat(force=True)  # 气泡内容被改写，必须整屏重绘
        self.render_sidebar()  # 列表预览同步变为“消息已撤回”

    def can_recall(self, entry, now=None):
        """是否可撤回：自己的、非系统、未撤回且未超出时间窗。"""
        if entry.get("recalled") or entry["system"] or entry.get("id") is None:
            return False
        if entry["sender"] != self.username:
            return False
        now = time.time() if now is None else now
        return (now - (entry.get("ts") or 0)) <= RECALL_WINDOW

    def recall_message(self, entry):
        if not self.can_recall(entry):
            messagebox.showinfo("无法撤回", "只能撤回自己刚刚发送的消息")
            return
        # 服务端确认后会广播 recalled，届时再更新界面（避免本地先改造成不一致）
        self.try_send({"type": "recall", "id": entry["id"]})

    def bubble_menu(self, event, entry):
        m = self.build_bubble_menu(entry)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def copy_message(self, entry):
        text = entry["text"] or ""
        if not text:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()  # 立即生效，窗口关闭后仍保留在剪贴板
        except Exception:
            pass

    def delete_message(self, entry):
        """本地删除一条消息（仅影响本机记录，不通知对方）。"""
        conv = self.active
        buf = self.buffers.get(conv) if conv else None
        if not buf:
            return
        for i, e in enumerate(buf):
            if e is entry:
                del buf[i]
                break
        else:
            return
        if self.typing_conv == conv and not buf:
            self.clear_typing()
        self.render_chat(force=True)  # 删了一条消息，后续气泡都变了
        self.render_sidebar()

    def meta_of(self, entry, sent):
        t = time.strftime("%H:%M", time.localtime(entry["ts"])) \
            if entry["ts"] else ""
        if entry["system"]:
            return t, C_PREVIEW
        if sent:
            # 已读双勾变为蓝色（仿 Telegram）
            if entry["status"] == "read":
                return f"{t}  ✓✓", C_BLUE
            return f"{t}  ✓", C_PREVIEW
        return t, C_PREVIEW

    # ---------------- 操作 ----------------
    def refresh_lists(self):
        if self.sock is None:
            return
        self.try_send({"type": "list_users"})
        self.try_send({"type": "list_groups"})

    def on_header_click(self, _event=None):
        """点击会话头部：群聊打开成员列表，私聊给个提示。"""
        if self.active_type == "group" and self.active:
            self.show_members()
        elif self.active:
            messagebox.showinfo("会话信息", f"与 {self.active} 的私聊")

    def show_members(self):
        """群成员列表弹窗（仿 Telegram 的成员列表）。"""
        g = self.active
        if not g or self.active_type != "group":
            return
        win = tk.Toplevel(self.root)
        win.title(f"群成员 · {g}")
        win.configure(bg=C_BG)
        win.geometry("280x320")
        win.transient(self.root)
        tk.Label(win, text=g, font=("Microsoft YaHei", 12, "bold"), fg=C_BLUE,
                 bg=C_BG).pack(anchor="w", padx=14, pady=(12, 4))
        lst = tk.Listbox(win, font=("Microsoft YaHei", 10), relief="flat",
                         highlightthickness=0, activestyle="none")
        lst.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        tk.Button(win, text="邀请成员", command=self.invite_member, bg=C_BLUE,
                  fg="white", relief="flat", activebackground=C_BLUE,
                  activeforeground="white").pack(fill="x", padx=14, pady=(0, 12))
        self.try_send({"type": "group_members", "group": g})  # 拉最新成员
        self.members_win, self.members_list, self.members_conv = win, lst, g
        self.fill_members(lst, g)

    def fill_members(self, lst, g):
        members = self.group_members.get(g) or []
        try:
            lst.delete(0, tk.END)
            for m in sorted(members, key=lambda x: (x != self.username, x)):
                tags = []
                if m == self.username:
                    tags.append("我")
                tags.append("在线" if self.presence.get(m) else "离线")
                lst.insert(tk.END, f"{m}（{'，'.join(tags)}）")
            if not members:
                lst.insert(tk.END, "（正在获取成员…）")
        except tk.TclError:
            pass

    def refresh_members_panel(self, g):
        """成员弹窗开着且正是这个群时，同步刷新列表。"""
        win, lst = self.members_win, self.members_list
        if win is None or lst is None or self.members_conv != g:
            return
        try:
            if not win.winfo_exists():
                self.members_win = self.members_list = None
                return
        except tk.TclError:
            self.members_win = self.members_list = None
            return
        self.fill_members(lst, g)

    def open_discover(self):
        """发现群聊：列出服务器上的所有群组，可一键加入（仿 Telegram 公开群）。"""
        win = self.discover_win
        if win is not None and win.winfo_exists():
            win.lift()
            self.try_send({"type": "list_all_groups"})
            return
        win = tk.Toplevel(self.root)
        self.discover_win = win
        win.title("发现群聊")
        win.configure(bg=C_BG)
        win.geometry("320x320")
        win.transient(self.root)
        tk.Label(win, text="可加入的群聊", font=("Microsoft YaHei", 12, "bold"),
                 fg=C_BLUE, bg=C_BG).pack(anchor="w", padx=14, pady=(12, 6))
        self.discover_list = tk.Listbox(win, font=("Microsoft YaHei", 10),
                                        relief="flat", highlightthickness=0,
                                        activestyle="none")
        self.discover_list.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        bar = tk.Frame(win, bg=C_BG)
        bar.pack(fill="x", padx=14, pady=(0, 12))
        tk.Button(bar, text="加入", command=self.join_selected_group,
                  bg=C_BLUE, fg="white", relief="flat",
                  activebackground=C_BLUE, activeforeground="white"
                  ).pack(side="right")
        tk.Button(bar, text="刷新", relief="flat", bg="#f0f2f5",
                  command=lambda: self.try_send({"type": "list_all_groups"})
                  ).pack(side="right", padx=8)
        self.discover_list.bind("<Double-Button-1>",
                                lambda e: self.join_selected_group())
        self.on_all_groups(self.discover_groups)  # 先用缓存填充，避免空白
        self.try_send({"type": "list_all_groups"})  # 再拉最新

    def on_all_groups(self, groups):
        """服务器返回的全部群组（含人数与是否已加入），刷新发现窗口。"""
        self.discover_groups = [g for g in groups if isinstance(g, dict)
                               and isinstance(g.get("name"), str)]
        win, lst = self.discover_win, self.discover_list
        if lst is None or win is None or not win.winfo_exists():
            return
        try:
            lst.delete(0, tk.END)
            for g in self.discover_groups:
                mark = "（已加入）" if g.get("joined") else ""
                lst.insert(tk.END, f"{g['name']}  ·  {g.get('count', 0)} 人{mark}")
            if not self.discover_groups:
                lst.insert(tk.END, "（暂无群聊，去新建一个吧）")
        except tk.TclError:
            pass

    def join_selected_group(self):
        lst = self.discover_list
        if lst is None:
            return
        sel = lst.curselection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一个群聊")
            return
        idx = int(sel[0])
        if idx >= len(self.discover_groups):  # 选中了“暂无群聊”等提示行
            return
        info = self.discover_groups[idx]
        g = info["name"]
        if info.get("joined"):
            messagebox.showinfo("提示", f"你已经在该群聊「{g}」中")
            return
        if self.try_send({"type": "group_join", "group": g}):
            win = self.discover_win
            if win is not None and win.winfo_exists():
                win.destroy()  # 加入成功会推送群信息，届时自动出现在会话列表
            self.discover_win = None
            self.discover_list = None

    def invite_member(self):
        """群聊邀请新成员（复用 group_create：同名群组会自动合并成员）。"""
        if self.sock is None:
            return
        if not self.active or self.active_type != "group":
            messagebox.showinfo("提示", "请先进入一个群聊再邀请成员")
            return
        users = simpledialog.askstring("邀请成员", "用户名(逗号分隔):")
        mems = [m.strip() for m in (users or "").split(",") if m.strip()]
        if not mems:
            return
        self.try_send({"type": "group_create",
                       "group": self.active, "members": mems})

    def new_group(self):
        if self.sock is None:
            return
        g = simpledialog.askstring("新建群聊", "群组名称:")
        if not g:
            return
        g = g.strip()
        if not valid_name(g):
            messagebox.showerror("群名不合法",
                                 f"群名需为 1~{MAX_NAME} 个可见字符")
            return
        members = simpledialog.askstring("添加成员",
                                         "成员用户名(逗号分隔，可留空):")
        mems = [m.strip() for m in (members or "").split(",") if m.strip()]
        self.try_send({"type": "group_create",
                       "group": g, "members": mems})

    def send_msg(self):
        if self.sock is None or not self.active:
            return
        text = self.input.get("1.0", tk.END).strip()
        if not text or text == "消息":
            return
        if len(text) > MAX_TEXT:  # 与服务端一致的上限，避免发出去被丢弃
            messagebox.showwarning("消息过长",
                                   f"单条消息最多 {MAX_TEXT} 个字符，"
                                   f"当前 {len(text)} 个")
            return
        if self.active_type == "user":
            ok = self.try_send({"type": "chat", "to": self.active,
                                "text": text})
        else:
            ok = self.try_send({"type": "group_msg",
                                "group": self.active, "text": text})
        if not ok:
            return  # 断线发送失败：保留草稿，重连后可直接重发
        self.input.delete("1.0", tk.END)
        if self.root.focus_get() is not self.input:  # 失焦时恢复占位提示
            self.input.insert("1.0", "消息")
            self.input.configure(fg="#9a9a9a")
        self.adjust_input_height()

    def on_input_return(self, event):
        # Enter 发送；Shift+Enter 换行
        if event.state & 0x1:
            return
        self.send_msg()
        return "break"

    def adjust_input_height(self, event=None):
        # 根据内容行数自动增高（1~5 行）
        lines = int(self.input.index("end-1c").split(".")[0])
        self.input.configure(height=min(max(lines, 1), 5))

    def on_input_focus_in(self, event):
        if self.input.get("1.0", tk.END).strip() == "消息":
            self.input.delete("1.0", tk.END)
            self.input.configure(fg="#000000")

    def on_input_focus_out(self, event):
        if not self.input.get("1.0", tk.END).strip():
            self.input.insert("1.0", "消息")
            self.input.configure(fg="#9a9a9a")

    def on_key_typing(self, event):
        if not self.active or self.sock is None:
            return
        now = time.time()
        if now - self.last_typing < 2.5:  # 节流：最多每 2.5s 发一次
            return
        self.last_typing = now
        out = {"type": "typing", "conv": self.active}
        if self.active_type == "group":
            out["group"] = self.active
        self.try_send(out)

    # ---------------- 消息处理 ----------------
    def poll(self):
        while True:
            try:
                msg = self.queue.get_nowait()
            except queue.Empty:
                break
            try:
                self.handle_msg(msg)  # 单条消息异常不应中断整个轮询
            except Exception as e:
                # 完全静默会掩盖 bug，这里保留最小可观测性
                print(f"[!] 处理消息失败 type={msg.get('type')!r}: {e!r}")
        if self.closing:  # 窗口已关闭，停止调度
            return
        try:
            self.root.after(100, self.poll)
        except Exception:
            pass

    def handle_msg(self, msg):
        t = msg.get("type")
        if t == "login_ok":
            self.last_pong = time.time()   # 刚登录成功，链路必然是通的
            self.start_heartbeat()
            if not self.main_built:
                self.build_main()
            else:  # 重连成功：沿用原界面，只恢复状态
                self.reconnecting = False
                if hasattr(self, "hs"):
                    self.hs["text"] = "在线"
                    self.hs["fg"] = "#3aaf5c"
                self.update_header()
        elif t == "login_fail":
            self.closing = True  # 本次连接作废，释放 socket，避免线程泄漏
            self.reconnecting = False
            if self.sock is not None:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None
            messagebox.showerror("登录失败", msg.get("reason", ""))
        elif t == "user_list":
            users = [u for u in msg.get("users", [])
                     if isinstance(u, str) and u != self.username]
            # 先全部置为离线，再按列表置为在线，避免重连后残留过期在线状态
            for u in self.presence:
                self.presence[u] = False
            for u in users:
                self.ensure_conv(u, "user")
                self.presence[u] = True
            self.presence[self.username] = True
            self.render_sidebar()
        elif t == "group_list":
            for g in msg.get("groups", []):
                if isinstance(g, str) and g:
                    self.ensure_conv(g, "group")
            self.render_sidebar()
        elif t == "presence":
            u = msg.get("user")
            if isinstance(u, str):
                self.presence[u] = bool(msg.get("online"))
                self.render_sidebar()  # 刷新会话头像上的在线状态点
                if self.active == u:
                    self.update_header()
                if self.members_conv:
                    self.refresh_members_panel(self.members_conv)
        elif t == "typing":
            self.on_typing(msg.get("from"), msg.get("conv"))
        elif t == "search_result":
            self.on_search_result(msg.get("users", []))
        elif t == "group_info":
            g = msg.get("group")
            if isinstance(g, str):
                self.group_members[g] = [m for m in msg.get("members", [])
                                         if isinstance(m, str)]
                if g not in self.conv_type:  # 被拉进新群时自动建会话
                    self.ensure_conv(g, "group")
                    self.render_sidebar()
                if self.active == g:
                    self.update_header()
                self.refresh_members_panel(g)
        elif t == "private":
            frm = msg.get("from")
            outgoing = (frm == self.username)
            peer = msg.get("to") if outgoing else frm
            if isinstance(peer, str) and isinstance(frm, str):
                self.store_message(peer, frm, msg.get("text") or "",
                                   msg.get("ts"), "user", msg.get("id"),
                                   outgoing=outgoing,
                                   replay=msg.get("replay", False))
        elif t == "group":
            g = msg.get("group")
            frm = msg.get("from")
            if isinstance(g, str) and isinstance(frm, str):
                self.store_message(g, frm, msg.get("text") or "",
                                   msg.get("ts"), "group", msg.get("id"),
                                   outgoing=(frm == self.username),
                                   replay=msg.get("replay", False))
        elif t == "pong":  # 心跳应答：链路正常
            self.last_pong = time.time()
        elif t == "all_groups":
            self.on_all_groups(msg.get("groups", []))
        elif t == "recalled":
            self.on_recalled(msg.get("id"), msg.get("conv"), msg.get("from"))
        elif t == "read_receipt":
            self.mark_read(msg.get("target"), msg.get("id"))
        elif t == "system":
            text = msg.get("text") or ""
            conv = msg.get("conv")  # 服务器可指定归属会话
            if isinstance(conv, str) and conv:
                ctype = self.conv_type.get(conv, "group")
                self.store_message(conv, "系统", text,
                                   msg.get("ts"), ctype, system=True)
            elif self.active:
                self.store_message(self.active, "系统", text,
                                   msg.get("ts"), self.active_type,
                                   system=True)
            else:
                messagebox.showinfo("系统消息", text)
        elif t == "left_group":
            self.delete_conv(msg.get("group"), confirm=False)
        elif t == "kicked":
            self.on_kicked(msg.get("reason"))
        elif t == "disconnected":
            self.on_conn_lost()  # 尝试自动重连（仿 Telegram）

    def store_message(self, conv, sender, text, ts, ctype, mid=None,
                      outgoing=False, system=False, replay=False):
        # 按 id 去重：重连/补投的历史可能与已收到消息重复
        # 用索引集合做 O(1) 判定，避免消息一多就退化成“每条都扫全表”
        seen = self.seen_ids.setdefault(conv, set())
        if mid is not None:
            if mid in seen:
                return
            seen.add(mid)
        self.ensure_conv(conv, ctype)
        entry = {
            "id": mid,
            "sender": sender,
            "text": text,
            "ts": ts,
            "system": system,
            # 自己的消息初始为“已发送”(单勾)，收到已读回执后变双勾
            "status": "sent" if outgoing else None,
            "receipt_sent": False,
        }
        buf = self.buffers.setdefault(conv, [])
        buf.append(entry)
        if len(buf) > BUFFER_LIMIT:  # 只保留最近若干条，防止长时间聊天越来越卡
            dropped = buf[:len(buf) - BUFFER_LIMIT]
            del buf[:len(buf) - BUFFER_LIMIT]
            for old in dropped:
                if old.get("id") is not None:
                    seen.discard(old["id"])
        self.move_to_front(conv)
        if self.typing_conv == conv and not replay:
            self.clear_typing()  # 收到消息说明对方停止输入
        if self.active == conv and not outgoing and not system and not replay:
            self.send_read_receipts(conv)  # 正在查看则自动回执已读
        if self.active == conv:
            # 自己发送的消息始终滚到底；收到的消息仅当原在底部时才跟随
            self.render_chat(force_bottom=outgoing)
        elif not replay:
            # 回放的历史不计未读，避免登录后满屏红点
            self.unread[conv] = self.unread.get(conv, 0) + 1
            if not system:
                self.notify_new_message()
        self.render_sidebar()
        self.update_header()  # 收到消息时清除“正在输入”提示

    def send_read_receipts(self, conv):
        """把本会话中尚未回执的来消息标记为已读，并通知对方。

        积压很多未读时（比如刚打开一个很久没看的会话）合并成一条批量回执，
        避免瞬间涌出几百个报文；服务端会逐条校验后再转发。
        """
        if self.sock is None:
            return
        pending = []
        for entry in self.buffers.get(conv, []):
            if entry["system"] or entry["sender"] == self.username:
                continue
            if entry["receipt_sent"] or entry["id"] is None:
                continue
            pending.append(entry)
        if not pending:
            return
        if len(pending) == 1:
            self.try_send({"type": "read", "target": conv,
                           "id": pending[0]["id"],
                           "to": pending[0]["sender"]})
        else:
            self.try_send({"type": "read", "target": conv,
                           "ids": [e["id"] for e in pending]})
        for entry in pending:  # 本地先记下，避免重发
            entry["receipt_sent"] = True

    def mark_read(self, conv, mid):
        """把 id=mid 的自发消息标为已读；conv 不匹配时按 id 全局兜底查找。"""
        if mid is None:
            return
        hit = None
        for entry in self.buffers.get(conv, []):
            if entry["id"] == mid and entry["sender"] == self.username:
                hit = entry
                break
        if hit is None:  # 兜底：不同版本/不同视角的会话名可能对不上，按 id 找
            conv = None
            for name, buf in self.buffers.items():
                for entry in buf:
                    if entry.get("id") == mid and entry["sender"] == self.username:
                        hit = entry
                        conv = name
                        break
                if hit is not None:
                    break
        if hit is None or hit.get("status") == "read":
            return
        hit["status"] = "read"
        if self.active == conv and not self.refresh_meta(conv, hit):
            self.render_chat(force=True)

    def refresh_meta(self, conv, entry):
        """只更新某条消息的“时间/已读勾”标签；控件不在屏上时返回 False。"""
        widgets = self._meta_widgets.get(conv)
        if not widgets:
            return False
        label = widgets.get(entry.get("id"))
        if label is None:
            return False
        try:
            if not label.winfo_exists():
                return False
            text, fg = self.meta_of(entry, entry["sender"] == self.username)
            label.configure(text=text, fg=fg)
        except tk.TclError:
            return False
        return True


def main():
    root = tk.Tk()
    ChatClient(root)
    root.mainloop()


if __name__ == "__main__":
    main()
