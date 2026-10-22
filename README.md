# MoyuChat

> 基于 TCP/IP 的轻量级即时通讯软件，参考 Telegram 的聊天体验，支持桌面客户端（Tkinter）与网页客户端（React），并可在局域网内多机、甚至多服务器（联邦）下互通。

---

## 一、项目简介与目标

**MoyuChat** 是一个用于学习与演示的即时通讯（IM）项目，力求用**最少的依赖**实现一个体验完整、可真实联机的聊天系统。

设计目标：

- **零强制第三方依赖**：核心服务端 / 桌面客户端仅用 Python 标准库即可运行，开箱即用。
- **贴近主流 IM 体验**：单/双勾已读回执、在线状态、正在输入、未读红点、群聊、历史同步、消息撤回等。
- **多端互通**：同一套协议同时支撑 Tkinter 桌面端、React 网页端，以及未来的更多客户端。
- **可部署**：支持局域网一键起服、UDP 自动发现、网页生产托管，以及多服务器联邦互联。
- **可演进**：持久化层用依赖倒置（`BaseStorage`）抽象，JSON 与 MySQL 实现可热切换；协议用「长度前缀 + JSON」简单可靠。

---

## 二、功能特性概述

### 账号与会话

- 用户名登录（服务器校验重名）；同名互踢（新登录顶掉旧连接，旧连接停止自动重连回到登录界面）。
- 会话管理：右键「置顶 / 删除会话」；置顶始终排在最前；群聊可「邀请成员 / 退出群组」。
- 未读数上限 `99+`；列表预览显示「正在输入…」（蓝色高亮）；搜索会话名与历史消息内容均可命中。

### 聊天

- 私聊 / 群聊：创建群组、邀请在线成员、加入群组、群消息广播。
- 群聊气泡显示发送者彩色昵称与头像；群会话头显示成员数。
- 群组历史消息同步（进入群组时自动拉取）。
- 消息操作：右键气泡可「复制 / 删除（仅本机）/ 撤回」；撤回限本人、限时（默认 120 秒，`MOYU_RECALL_WINDOW` 可调），双方同步显示「消息已撤回」且落盘为墓碑。
- 多行输入框：Enter 发送，Shift+Enter 换行，输入随内容自动增高；断线重连后草稿可重发。

### 实时与状态

- 在线 / 离线状态实时同步，会话头显示对方状态。
- 消息回执：发送成功单勾 `✓`，对方已读蓝色双勾 `✓✓`（可批量合并回执）。
- 正在输入提示（仿 Telegram）；日期分隔条（今天 / 日期）。
- 智能滚动：仅当用户位于底部时自动跟随新消息，不打断历史阅读。

### 数据与健壮性

- 数据持久化：服务端将群组、群历史、私聊历史、离线私聊落盘（默认本地 JSON），重启不丢；登录回放历史带 `replay` 标记（不计未读）。
- 离线消息：对方离线时缓存，上线后自动投递。
- 断线自动重连并重新登录，界面与聊天记录保留。
- 健壮性约束：用户名/群名限长且拒绝控制字符；单条消息长度上限（默认 4096，`MOYU_MAX_TEXT`）；离线消息箱上限；TCP keepalive + 应用层心跳；限流防刷屏/DoS。
- 性能：本地按 id 去重、每会话最多缓存若干条；聊天区只渲染最近一屏，更早消息可「载入更早的消息」增量展开，长时间使用不卡顿。

### 网络与部署

- 局域网多人聊天（服务器绑定 `0.0.0.0`）；UDP 广播自动发现（桌面端免手输 IP）。
- 网页端经 WebSocket 桥接复用同一 TCP 服务器。
- 服务器间**联邦（federation）**：跨服务器私聊中继、在线状态与发现同步。
- 可切换 MySQL 存储；可打包为免安装 `exe`。

---

## 三、系统架构与通信协议

### 架构总览

```
 桌面客户端(client.py/Tkinter) ─┐
                                ├─ TCP(9000) ──► server.py(asyncio) ──┬─ storage(Json/MySQL)
 网页客户端(web/React) ─► ws_bridge.py(9106) ┘                        ├─ discover.py(UDP发现)
                                                                        └─ fed.py(联邦 9100) ─► 其他服务器
```

- **桌面端**直接用 TCP 连接服务器（端口 `MOYU_PORT`，默认 9000）。
- **网页端**因浏览器无法建裸 TCP，先连 `ws_bridge.py`（默认 `ws://127.0.0.1:9106/ws`），由桥接把 WebSocket(JSON 文本帧) 与服务器要求的「4 字节长度前缀 + JSON」TCP 帧互转。
- **局域网发现**：服务器开启 UDP 广播应答（端口 `MOYU_DISCOVERY_PORT`，默认 9109），桌面端登录时自动扫描预填服务器地址（浏览器无法发 UDP 广播，故网页端仍需用 URL）。
- **联邦**：多台服务器经 `fed.py` 两两建链，实现跨服务器私聊中继与状态同步。

### 报文格式

基于 TCP 的「长度前缀 + JSON」：

```
| 4 字节大端长度 N | N 字节 UTF-8 编码的 JSON |
```

长度前缀保证接收方能完整切分出一个 JSON 对象，避免粘包/半包。

### 主要报文类型

| 方向 | type | 说明 |
| --- | --- | --- |
| C→S | `login` | 登录，携带 `username` |
| C→S | `chat` | 私聊，携带 `to` / `text` |
| C→S | `group_create` | 建群；群已存在时等价于「成员邀请」（仅限群成员，携带 `group` / `members`） |
| C→S | `group_join` | 加入群组，携带 `group` |
| C→S | `group_leave` | 退出群组，携带 `group` |
| C→S | `group_msg` | 群消息，携带 `group` / `text` |
| C→S | `recall` | 撤回消息，携带 `id`（仅能撤回自己、限时内的消息） |
| C→S | `read` | 已读回执，携带 `target` / `id`（或 `ids` 批量）/ `to` |
| C→S | `typing` | 正在输入，携带 `conv`（群聊附带 `group`） |
| C→S | `list_users` / `list_groups` | 拉取列表（`list_groups` 只含本人所属群） |
| C→S | `list_all_groups` | 拉取全部群聊概况（“发现群聊”用） |
| C→S | `group_members` | 按需拉取群成员（仅群内成员可查） |
| S→C | `login_ok` / `login_fail` | 登录结果 |
| S→C | `kicked` | 本会话被同名登录顶掉，携带 `reason`（客户端据此停止自动重连） |
| S→C | `private` / `group` | 聊天消息（含 `id` 用于回执；回放历史带 `replay`） |
| S→C | `read_receipt` | 已读回执，携带 `target` / `id` |
| S→C | `recalled` | 撤回通知，携带 `id` / `conv`（客户端据此置为「消息已撤回」） |
| S→C | `left_group` | 退群成功，客户端据此移除会话 |
| S→C | `presence` | 在线状态，携带 `user` / `online` |
| S→C | `group_info` | 群成员列表，携带 `group` / `members` |
| S→C | `typing` | 正在输入，携带 `conv` / `from` |
| S→C | `user_list` / `group_list` | 列表同步（`group_list` 仅含本人所属群） |
| S→C | `all_groups` | 全部群聊概况：`[{name, count, joined}]` |
| S→C | `system` | 系统提示（可带 `conv` 指定归属会话） |

> 更精确的线格式见 `test_ws_bridge.py`（端到端验证脚本）与 `web/` 源码。

---

## 四、项目文件与目录结构

### 目录树

```
moyu/
├── README.md                # 本文档
├── requirements.txt         # 可选第三方依赖（MySQL / WebSocket 桥接）
├── common.py                # 共享通信工具与协议常量（被所有模块复用）
├── server.py                # 核心 TCP 服务器（asyncio）
├── client.py                # Tkinter 桌面客户端
├── storage.py               # 持久化层（JsonStorage / MysqlStorage）
├── ws_bridge.py             # WebSocket ↔ TCP 帧桥接（网页端用）
├── discover.py              # UDP 广播自动发现（应答器 + 客户端扫描）
├── fed.py                   # 服务器间联邦引擎
├── serve_web.py             # 生产静态托管 + 内置 WS 桥接（纯标准库）
├── server_app.py            # 一体化服务器（TCP+UDP发现+Web 单进程）
├── start_lan.py             # 局域网一键启动（server + web）
├── make_icon.py             # 生成应用图标 assets/icon.ico
├── MoyuChat.spec            # 桌面客户端 PyInstaller 打包配置
├── MoyuChatServer.spec      # 一体化服务器 exe PyInstaller 打包配置
├── commit_msg.txt           # 提交信息模板（未跟踪）
├── test_*.py                # 各维度自动化测试
├── assets/                  # 图标资源（icon.ico / icon.png）
└── web/                     # React + TDesign 网页前端
    ├── package.json
    ├── vite.config.ts
    ├── tsconfig.json
    ├── index.html
    ├── README.md
    ├── smoke/               # jsdom 无浏览器冒烟测试
    └── src/
        ├── main.tsx        # 入口，加载 TDesign 样式
        ├── App.tsx         # 登录 / 主界面切换
        ├── types.ts        # 协议类型定义
        ├── net/transport.ts        # WebSocket 传输层（自动重连 + 心跳）
        ├── store/chatStore.ts      # 全局状态 + 协议分发 + 动作
        ├── utils/util.ts           # 头像配色 / 时间格式化
        ├── styles/app.css          # 浅色主题样式
        └── components/
            ├── Login.tsx           # 登录页（登录即注册）
            ├── MainLayout.tsx      # 侧栏 + 聊天区布局
            ├── ConversationList.tsx# 会话列表（搜索/未读/在线/右键菜单）
            ├── ChatPanel.tsx       # 聊天面板（头部/消息/输入）
            ├── MessageBubble.tsx   # 气泡（复制/撤回/删除）
            └── Dialogs.tsx         # 新建群聊/发现群聊/成员列表
```

### 核心文件职责与相互关系

**共享基座**

- `common.py` —— 所有模块的公共依赖。定义：
  - `send_obj` / `recv_obj`：基于「4 字节长度前缀 + JSON」的收发原语；
  - 协议常量：`HOST` / `PORT` / `BIND_HOST` / `MAX_FRAME` / `MAX_TEXT` / `MAX_NAME` / `RECALL_WINDOW` / `PING_INTERVAL` / `PING_TIMEOUT`（均可用同名 `MOYU_*` 环境变量覆盖）；
  - `lan_ip()`：探测本机局域网出口 IP；`valid_name()`：校验用户名/群名（限长且拒绝控制字符，防止会话键冲突）。
  - 被 `server.py`、`client.py`、`ws_bridge.py`、`discover.py`、`fed.py`、`serve_web.py`、`server_app.py`、`start_lan.py` 共同引用。

**服务端**

- `server.py` —— 基于 `asyncio` 的 TCP 服务器，是系统的“大脑”：
  - 维护在线连接、私聊点对点路由、群聊创建/加入/广播；
  - 在线用户/群组列表同步、离线消息缓存与上线投递、历史回放；
  - 心跳保活（`sweep_dead` 清理无心跳连接）、限流（`RATE_LIMIT`）、消息 ID 采用全局唯一 UUID 发号（避免联邦跨服务器时各服务器自增 ID 相互覆盖导致回执/撤回命中错消息）；
  - 依赖 `common.py`、`storage.py`、`discover.py`、`fed.py`。
- `storage.py` —— 持久化层，通过 `BaseStorage` 抽象 + `create_storage()` 工厂实现**依赖倒置**：
  - `JsonStorage`（默认，写入 `data/store.json`，零依赖）；
  - `MysqlStorage`（设置 `MOYU_STORAGE=mysql` 启用）；
  - `server.py` 只依赖抽象接口，切换存储无需改业务代码。
- `fed.py` —— 联邦引擎：让多台 `server.py` 两两建链，实现跨服务器私聊中继、在线状态/发现同步、读回执与撤回跨服同步，含指数退避重连与发件箱补投。
- `discover.py` —— UDP 自动发现：`run_responder_thread` 在服务器侧应答；`discover_sync` 在桌面客户端侧广播探测并收集服务器地址，免去手动输入 IP。

**网页链路**

- `ws_bridge.py` —— 浏览器（WebSocket / JSON 文本帧）与 TCP 服务器（长度前缀帧）之间的双向翻译器，每个浏览器连接对应一个到后端的 TCP 连接。
- `serve_web.py` —— **纯标准库**的生产托管：同时提供 `web/dist` 静态文件与内置 WS 桥接，单端口（默认 8080）即可运行网页端，无需 Node/Vite。
- `server_app.py` —— 一体化服务器：单进程同时拉起 TCP 聊天（9000）+ UDP 发现（9109）+ 网页托管/WS 桥接（8080/9106），适合直接发给他人一键起服。
- `start_lan.py` —— 开发/生产一键启动脚本：同时拉起 `server.py` 与网页端（开发模式用 Vite，或 `--prod` 用 `serve_web.py`），并打印本机局域网 IP。

**客户端与资源**

- `client.py` —— Tkinter 桌面客户端（Telegram 风格界面），仅用标准库（tkinter/socket/threading/queue/ctypes/json），不依赖 PyMySQL/websockets，因此可独立打包而不带入服务端依赖。
- `make_icon.py` —— 用 Pillow 生成多分辨率 `assets/icon.ico`，供 PyInstaller 打包使用。
- `assets/` —— 图标资源（`icon.ico` 由 `make_icon.py` 生成，`icon.png` 为源图）。
- `web/` —— React 18 + TDesign + Vite 网页前端，复用同一协议；详见 `web/README.md`。与服务端的关系：`client.py` 是旧版桌面端，`web/` 是响应式网页版，二者共用同一服务器与协议。

**打包配置**

- `MoyuChat.spec` —— 把 `client.py` 打成单文件 `MoyuChat.exe`（约 9MB，免安装）。
- `MoyuChatServer.spec` —— 把「TCP 聊天 + UDP 发现 + 网页托管」打成 `MoyuChatServer.exe`。

**测试**

- `test_smoke.py` —— 基础冒烟（连接已运行的服务器或自拉起）。
- `test_full.py` —— 全功能模拟（多用户：私聊/群聊/回执/离线/持久化，独立端口 9100）。
- `test_ui.py` —— 无头 Tk 交互测试（会话点击/发送/右键菜单/已读/输入中/重连）。
- `test_robust.py` —— 健壮性测试（畸形/超长/半截报文、越权操作，独立端口 9101）。
- `test_e2e.py` —— GUI 端到端（真实 Tk 客户端 + 真实服务器，端口 9103）。
- `test_soak.py` —— 压力/浸泡（900 群消息 + 400 私聊，校验不丢不重、ID 唯一、落盘一致，端口 9105）。
- `test_ws_bridge.py` —— WebSocket 桥接端到端验证（线格式）。
- `test_discover.py` —— UDP 自动发现验证。
- `test_fed.py` —— 联邦（跨服务器）验证。
- `test_mysql.py` —— MySQL 存储层单测（独立库 `moyu_chat_test`）。
- `test_mysql_e2e.py` —— 真实服务器 + MySQL 端到端（落库 + 重启回放，端口 9107 / 库 `moyu_chat_e2e`）。

---

## 五、环境要求与依赖

| 组件 | 要求 |
| --- | --- |
| Python | 3.8+（服务端 / 桌面端 / 桥接，标准库即可运行） |
| Node.js | ≥ 18（仅网页端开发/构建时需要） |
| MySQL | 5.5+（**可选**，仅当使用 `MOYU_STORAGE=mysql` 时需要） |
| 操作系统 | Windows / Linux / macOS（桌面端依赖 tkinter，通常随 Python 分发） |

### 依赖项

- **核心（默认零依赖）**：服务端 `server.py`、桌面端 `client.py`、桥接 `ws_bridge.py` 之外的多数模块仅用标准库。
- **可选第三方依赖**（`requirements.txt`）：
  - `websockets>=12.0` —— 运行 `ws_bridge.py` 时需要；
  - `PyMySQL>=1.0` —— 使用 MySQL 存储时需要；
  - `Pillow` —— 运行 `make_icon.py` 生成图标时需要（打包 exe 时用到）。

```bash
pip install -r requirements.txt   # 安装全部可选依赖
```

---

## 六、安装与配置

### 1. 获取代码

```bash
git clone <本仓库地址>
cd moyu
```

### 2. 安装依赖（按需）

```bash
pip install -r requirements.txt   # MySQL 与 WS 桥接支持
# 网页端（开发模式）：
cd web && npm install && cd ..
```

### 3. 常用环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MOYU_HOST` | `127.0.0.1` | 客户端默认连接地址 |
| `MOYU_PORT` | `9000` | 服务器 TCP 监听端口 |
| `MOYU_BIND_HOST` | `0.0.0.0` | 服务器绑定地址（含局域网） |
| `MOYU_MAX_FRAME` | `16777216` | 单帧最大字节数（16MB，防内存暴涨） |
| `MOYU_MAX_TEXT` | `4096` | 单条消息最大字符数 |
| `MOYU_MAX_NAME` | `24` | 用户名/群名长度上限 |
| `MOYU_RECALL_WINDOW` | `120` | 消息可撤回时间窗（秒） |
| `MOYU_PING_INTERVAL` | `30` | 心跳间隔（秒） |
| `MOYU_PING_TIMEOUT` | `100` | 心跳超时（秒，超时判死链重连） |
| `MOYU_DISCOVERY_PORT` | `9109` | UDP 自动发现端口 |
| `MOYU_WS_HOST` / `MOYU_WS_PORT` | `0.0.0.0` / `9106` | WS 桥接监听地址 |
| `MOYU_BACKEND_HOST` / `MOYU_BACKEND_PORT` | `127.0.0.1` / `9000` | 桥接/托管指向的 TCP 后端 |
| `MOYU_WEB_PORT` | `8080` | 生产网页托管端口 |
| `MOYU_STORAGE` | `json` | 存储实现：`json` 或 `mysql` |
| `MOYU_DB_HOST` / `MOYU_DB_PORT` / `MOYU_DB_USER` / `MOYU_DB_PASSWORD` / `MOYU_DB_NAME` / `MOYU_DB_CHARSET` | `127.0.0.1` / `3306` / `root` / `root` / `moyu_chat` / `utf8mb4` | MySQL 连接参数 |
| `MOYU_SERVER_ID` / `MOYU_FED_PORT` / `MOYU_FED_BIND` / `MOYU_PEERS` / `MOYU_FED_SECRET` / `MOYU_FED_ALLOW` | 见 `fed.py` | 联邦配置 |

> 跨机通信时，将 `common.py` 中的 `HOST` 改为服务器实际 IP，或用 `MOYU_HOST` 覆盖即可。

---

## 七、使用方式（典型示例）

### 示例 A：本机快速体验（桌面端）

```bash
python server.py        # 终端 1：启动服务器
python client.py        # 终端 2/3：启动客户端，输入不同用户名登录
```

### 示例 B：局域网多人聊天（推荐一键启动）

在“服务器那台机器”运行：

```bash
python start_lan.py
```

脚本会同时拉起服务器 + 网页前端，并打印本机局域网 IP，例如：

```
本机局域网 IP：10.137.132.144
服务器：10.137.132.144:9000     （桌面客户端登录框填这个 IP）
网页端：http://10.137.132.144:5173
```

- **其他人加入（桌面端）**：登录界面「服务器」栏填上面打印的 IP（端口默认 9000）；或点「🔍 自动发现服务器」自动扫描预填。
- **其他人加入（网页端）**：浏览器打开 `http://<上面打印的IP>:5173`。
- 继续用 MySQL：`MOYU_STORAGE=mysql python start_lan.py`。

### 示例 C：网页端开发 / 生产

开发模式（热更新，需 Node）：

```bash
python server.py
python ws_bridge.py
cd web && npm run dev     # http://localhost:5173
```

生产模式（运行时无需 Node）：

```bash
cd web && npm run build    # 生成 web/dist
python serve_web.py        # 或：python start_lan.py --prod
# 浏览器打开 http://<服务器IP>:8080
```

### 示例 D：切换 MySQL 存储

```bash
pip install -r requirements.txt
export MOYU_STORAGE=mysql     # Windows: set MOYU_STORAGE=mysql
python server.py
```

首次启动自动建库建表（无需手动执行 SQL）；连接参数可用上表 `MOYU_DB_*` 覆盖。

### 示例 E：多服务器联邦

假设局域网内有两台服务器，IP 分别为 `10.137.132.144`（A）与 `10.137.132.145`（B），各自跑一份 `server.py`，并让它们互相建链：

```bash
# 服务器 A（监听联邦端口 9100，主动拨号到 B）
MOYU_SERVER_ID=A MOYU_FED_PORT=9100 \
  MOYU_PEERS=10.137.132.145:9100 MOYU_FED_SECRET=共享密钥 python server.py

# 服务器 B（监听联邦端口 9100，主动拨号到 A）
MOYU_SERVER_ID=B MOYU_FED_PORT=9100 \
  MOYU_PEERS=10.137.132.144:9100 MOYU_FED_SECRET=共享密钥 python server.py
```

建链后，分属不同服务器的用户（如 A 上的 `alice` 与 B 上的 `carol`）可互相私聊，在线状态、已读回执、撤回、发现均跨服务器同步。更完整的设计、端口映射、防火墙与容错说明见「十三、服务器间联邦（跨服务器通信）」。

### 示例 F：打包为免安装 exe

```bash
pip install pyinstaller
pyinstaller MoyuChat.spec --noconfirm --clean        # 产出 dist/MoyuChat.exe（桌面客户端）
pyinstaller MoyuChatServer.spec --noconfirm --clean  # 产出 dist/MoyuChatServer.exe（一体化服务器）
```

- 桌面客户端目标机无需 Python，双击即用；登录界面自动扫描服务器（UDP 发现）。
- 一体化服务器 exe 运行后打印局域网 IP 与网页地址；前端静态文件已打进 exe（改前端需先 `cd web && npm run build` 再打包）。
- 图标：`python make_icon.py` 生成 `assets/icon.ico`（`UPX` 可选进一步压缩）。
- `dist/`、`build/` 已加入 `.gitignore`，不会把大体积产物提交进仓库。

---

## 八、数据存储与 MySQL 接口

服务端通过 `storage.create_storage()` 获取存储实现，默认 `JsonStorage`（写入 `data/store.json`）：

- `users`：曾登录过的用户名（防群名抢占同名会话键）；
- `groups`：`{群名: [成员...]}`；
- `group_history`：`{群名: [消息...]}`（最近 N 条）；
- `private_history`：`{"a\x1fb": [消息...]}`（键为双方用户名排序拼接，见 `storage.pair_key`）；
- `offline`：`{用户名: [私聊消息...]}`。

用户登录时回放历史，客户端按消息 `id` 去重，回放消息带 `replay=True`（不计未读、不回执）。重启后消息 ID 从历史上限继续发号。

切换到 MySQL：设置 `MOYU_STORAGE=mysql`（`MysqlStorage` 已实现），连参见上表。MySQL 中**全量留存**消息（`append` 不裁剪，可 SQL 统计），启动时仅载入每个会话最近若干条；兼容 MySQL 5.5+（`LONGTEXT` + 前缀索引规避 767 字节上限）；`server.py` 无需改动。

自测：`python test_mysql.py`、`python test_mysql_e2e.py`。

---

## 九、测试

```bash
python test_full.py      # 多用户全功能模拟（端口 9100，含重启持久化、同名重登）
python test_ui.py        # 无头 Tk 交互（点击/发送/右键/回执/输入中/重连）
python test_robust.py    # 健壮性（畸形/超长/半截报文、越权，端口 9101）
python test_e2e.py       # GUI 端到端（真实客户端 + 真实服务器，端口 9103）
python test_soak.py      # 压力浸泡（900 群 + 400 私聊，不丢不重，端口 9105）
python test_smoke.py     # 基础冒烟（复用已运行服务器或自拉起）
python test_ws_bridge.py # WebSocket 桥接线格式验证
python test_discover.py  # UDP 自动发现验证
python test_fed.py       # 联邦验证
```

网页端运行时验证（需先 `npm install`）：

```bash
cd web && npm run smoke   # jsdom 挂载真实 React 应用并喂入协议消息，校验渲染无异常
```

---

## 十、常见问题（FAQ）

**Q1：客户端连不上服务器？**
- 确认服务器已启动且端口（`MOYU_PORT`，默认 9000）一致；跨机时客户端 `MOYU_HOST` 填服务器实际 IP。
- 检查防火墙是否放行对应端口（9000 / 9106 / 5173，生产模式 8080）。
- 桌面端可用「🔍 自动发现服务器」自动扫描；网页端需直接用 `http://<IP>:<端口>` 访问。

**Q2：WebSocket 桥接连不上后端？**
- `ws_bridge.py` 默认指向 `127.0.0.1:9000`，需与 `server.py` 监听一致；可用 `MOYU_BACKEND_HOST/PORT` 覆盖。
- 确认已 `pip install websockets`。

**Q3：用 MySQL 时报连接/建表错误？**
- 确认 MySQL 已启动，且 `MOYU_DB_*` 参数正确；程序首次启动会自动建库建表，请确保账号有建库建表权限。

**Q4：消息撤回为什么有时失败？**
- 撤回仅限**本人发送**且在 `MOYU_RECALL_WINDOW`（默认 120 秒）内的消息；超时或对他人消息会失败。

**Q5：同名登录后旧窗口一直重连/互踢拉锯？**
- 这是预期防护：新登录会顶掉旧连接，旧连接收到 `kicked` 后停止自动重连并回到登录界面，避免两个客户端互相重连互相顶号。

**Q6：打包 exe 体积太大 / 想更小？**
- 桌面客户端已排除大量用不到的标准库模块瘦身；安装 UPX 后把 spec 中 `upx=False` 改为 `True` 可再压缩。
- 前端若改动，需先 `cd web && npm run build` 再重新打包服务器 exe。

**Q7：多网卡/IP 冲突，自动发现的 IP 不对？**
- `common.lan_ip()` 选本机出口网卡；若不准，可在客户端登录框手动改为目标 IP，或用 `MOYU_HOST` 覆盖。

---

## 十一、贡献指南

欢迎参与改进 MoyuChat！建议流程：

1. **Fork / 分支**：从主分支切出特性分支（如 `feature/xxx` 或 `fix/xxx`）。
2. **环境**：按「六、安装与配置」准备好 Python 与（网页端）Node 环境。
3. **开发与自测**：
   - 改动服务端/客户端后，跑对应测试（`test_full.py` / `test_robust.py` / `test_ui.py` 等）；
   - 改动网页端后跑 `cd web && npm run smoke`；
   - 涉及协议变更须同步更新 `common.py` 常量、`web/src/types.ts` 与本文档的报文表。
4. **代码风格**：
   - Python 端尽量维持“标准库优先、依赖倒置”的设计（新增存储/传输实现请走抽象接口）；
   - 提交信息清晰说明「做了什么 / 为什么」，可参考仓库内 `commit_msg.txt` 风格。
5. **提交与 PR**：
   - 提交前确保 `git status` 干净、测试通过；
   - 大体积产物（`dist/`、`build/`、`__pycache__/`、前端 `node_modules/`）不要提交（已在 `.gitignore`）；
   - 推送分支并发起 Pull Request，描述变更点与测试情况。
6. **Issue**：发现 bug 或建议新功能，请提交 Issue 并尽量附上复现步骤 / 日志 / 环境信息。

---

## 十三、服务器间联邦（跨服务器通信）设计与配置

本节回答「同一局域网内多台服务器之间如何实现客户端跨服务器通信」：角色与拓扑、客户端如何发现并联系另一台服务器的服务、路由/端口/地址映射、防火墙与访问控制、以及连接失败的排查与容错。

### 13.1 角色与网络拓扑

每台机器上运行一份 `server.py`，既服务本机登录的客户端，又作为**联邦对等节点（peer）**与其他服务器互联。所有服务器**地位对等**，无中心枢纽：

```
   客户端 alice ──TCP 9000──► 服务器 A (10.137.132.144)
                                    │ 联邦链路 (TCP 9100)
                                    ▼
   客户端 carol ──TCP 9000──► 服务器 B (10.137.132.145)
```

- **本服务器（home server）**：用户实际登录的那台。`alice` 登录在 A，A 负责维护 alice 的在线连接、私聊历史、回执与撤回。
- **对端服务器（peer）**：承载“对方用户”的那台。`carol` 在 B，因此 A 要把发往 carol 的消息经联邦链路转发给 B，由 B 投递给 carol。
- **联邦链路**：服务器之间一条长连接（A↔B 双向拨号，握手后去重只保留一条），承载 `private / presence / read_receipt / recall / query_user / query_result` 等“联邦帧”（报文带 `fed:true` 标记区分普通客户端帧）。

> 拓扑可扩展为星型/网状：只要每台的 `MOYU_PEERS` 列出想要互联的其它服务器 IP:端口即可；链路自动去重，不会因双向拨号产生两条冗余连接。

### 13.2 客户端如何发现并联系另一台服务器的服务

**发现（Discovery）**——客户端无需知道对方在哪台服务器：

1. 客户端在搜索框输入用户名并回车，发送 `query_user`（桌面端 `client.py` 与网页端 `ConversationList` 均已实现）。
2. 本服务器 `fed.client_query_user`：先查本地 `clients/users`，命中则直接返回（`remote:false`）；否则向**所有已连接对等服务器**广播 `fed_query_user + nonce`。
3. 各 peer 查本地，命中则回 `query_result{found:true, server:<peer_id>}`；发起方汇总后在约 1 秒内回客户端 `search_result`，逐项标注 `remote:true / server:B`。
4. 客户端弹「发现结果」：列出 `用户名 · 异地（服务器 B）`，点「聊天」即与对方开会话。

**联系（Contact）**——开聊后完全透明：

- 客户端照常发 `chat{to:"carol"}`。本服务器发现 `carol` 不在本地（既不在 `clients` 也不在 `users`），于是调用 `fed.deliver_or_queue`：
  - 若 `carol` 在 `fed.remote`（在线状态同步表）→ 直接经联邦链路转发；
  - 否则向各 peer 广播一次“在线查询”，命中则记下路由并转发，未命中则进入**发件箱（outbox）**排队，待对端上线/链路恢复后补投；都失败则给发送方一条 `system` 提示「当前不可达」。
- 对方客户端收消息、已读、撤回，均会沿原路回传：已读回执 `read_receipt` 与撤回 `recall` 经联邦回到发送者所在服务器，再投递给发送者本人，UI 表现与同服务器完全一致（双勾变蓝、显示「已撤回」）。

### 13.3 路由、端口与地址映射 / 代理转发

**端口规划**（每台服务器独立，互不影响）：

| 用途 | 变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| 客户端 TCP 聊天 | `MOYU_PORT` | `9000` | 桌面端直连 / 网页端经桥接 |
| 联邦监听（入站） | `MOYU_FED_PORT` | `9100` | 对端服务器拨入此端口 |
| 联邦拨号（出站） | `MOYU_PEERS` | 空 | `IP:PORT` 列表，逗号分隔 |
| UDP 自动发现 | `MOYU_DISCOVERY_PORT` | `9109` | 仅桌面端免手输 IP 用 |
| WS 桥接 / 网页托管 | `MOYU_WS_PORT` / `MOYU_WEB_PORT` | `9106` / `8080` | 网页端用 |

**地址映射 / 代理转发**三种常见情形：

- **纯局域网直连（推荐）**：各服务器用真实局域网 IP，防火墙放行后 `MOYU_PEERS` 直接填对方 IP:9100。无需额外 NAT/代理。
- **跨网段 / 有 NAT**：在边界路由器做**端口映射**，把公网或另一网段的 `:9100` 映射到目标服务器的 `MOYU_FED_PORT`；`MOYU_PEERS` 填映射后的可达地址。`MOYU_FED_BIND` 可绑定到特定网卡/地址（默认 `0.0.0.0`）。
- **经反向代理 / 隧道**：联邦是裸 TCP，可在两台服务器之间用 `socat` / `ssh -L` 等把一端 `9100` 隧道到另一端 `9100`，再在 `MOYU_PEERS` 填 `127.0.0.1:9100`。注意：联邦链路**不**走 WebSocket，不能用 WSS 反向代理；需 TCP 层隧道。

**鉴权**：联邦握手携带 `MOYU_FED_SECRET` 共享密钥（HMAC 挑战应答，明文仅比对哈希，不传输密钥）。`MOYU_FED_ALLOW` 可指定允许入站的对端 IP 白名单（逗号分隔），非白名单 IP 的连接在握手阶段直接拒绝，作为第一道访问控制。

### 13.4 防火墙与访问控制

- **放行端口**：每台服务器需放开 `MOYU_PORT`（客户端）、`MOYU_FED_PORT`（对端拨入）。网页端另需 `MOYU_WS_PORT`/`MOYU_WEB_PORT`，桌面端自动发现需 `MOYU_DISCOVERY_PORT`（UDP）。
  - Windows（管理员 PowerShell）：`New-NetFirewallRule -DisplayName "MoyuChat-Fed" -Direction Inbound -Protocol TCP -LocalPort 9100 -Action Allow`
  - Linux：`sudo ufw allow 9100/tcp`
- **最小暴露面**：联邦端口只应对“可信的对端服务器”开放；配合 `MOYU_FED_ALLOW` 白名单 + 共享 `MOYU_FED_SECRET`，即使端口暴露也不会被任意节点接入。
- **客户端与服务器之间**：沿用既有策略（9000/9106/8080 放行）；联邦是服务器↔服务器，客户端永远只连自己所在的 `home server`，不直接触达对端服务器。
- **传输安全**：当前联邦为明文 TCP（局域网演示场景可接受）。若跨不可信网络，请用 `ssh -L` / `socat` + TLS 隧道包裹 9100，或后续在 `fed.py` 的 `_server`/`_client` 接入 TLS（代码已预留 `ssl` 改造点）。

### 13.5 跨服务器连接失败的排查与容错

**常见故障与排查**：

| 现象 | 排查点 |
| --- | --- |
| 两台服务器互不“联邦” | 看启动日志是否出现 `[fed] 已与对端 ... 建立联邦链路`；检查 `MOYU_PEERS` 是否填对、对端 `MOYU_FED_PORT` 是否一致、TCP 9100 是否互通（`Test-NetConnection IP 9100`） |
| 能建链但跨服消息发不出 | 确认对方用户确实登录在其 home server 上（`query_user` 能否搜到 `remote` 结果）；检查 `MOYU_FED_SECRET` 两端是否一致（不一致握手失败会被拒） |
| 防火墙/白名单拦截 | 确认入站 9100 已放行；`MOYU_FED_ALLOW` 若设置，需包含对端 IP，否则握手即断开 |
| 仅单向可达（如 A 能拨 B，B 拨 A 不通） | 联邦支持单向拨号即可成链（去重只保留一条），因此单向互通也能工作；若要求双向冗余，需两端 9100 都可入站 |

**容错机制（已在 `fed.py` 实现）**：

1. **断线自动重连**：联邦链路断开后，出向拨号方按**指数退避**（1s→2s→4s…上限 30s）定时重试，无需人工干预；入站链路断开也会被清理并在对侧重连时恢复。
2. **在线状态自愈**：链路恢复瞬间，双方互相同步当前在线用户，路由表（`fed.remote`）自动重建，无需重启。
3. **发件箱补投（Outbox）**：消息发出时若对端临时不可达（链路抖动/对端刚重启），消息进入内存发件箱；链路恢复或对方上线后自动补投，发送方无感。
4. **优雅降级**：若目标用户在所有已连接服务器上都找不到（对端宕机且未恢复），发送方收到明确 `system` 提示「当前不可达」，而非静默丢失或崩溃。
5. **连接完整性**：联邦帧带 4 字节长度前缀 + JSON，与应用层心跳相同机制；半包/异常帧会被丢弃并触发重连，不会污染业务状态。

> 端到端验证见 `test_fed.py`：双服务器跨服私聊双向投递、跨服发现 `search_result`、已读回执跨服同步、对端宕机时发送方收到「不可达」提示、对端恢复后自动重连并恢复投递。

---

## 十二、许可证

本项目为学习/演示用途，许可证以仓库根目录 `LICENSE` 文件为准（若未提供，默认保留所有权利）。
