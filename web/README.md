# MoyuChat Web 前端（React + TDesign）

把 MoyuChat 的客户端用 **React 18 + TDesign + Vite** 重写，复用既有 Python 服务器
（`server.py`）。浏览器无法建立裸 TCP 连接，因此通过 `ws_bridge.py` 把 WebSocket 与
服务器要求的「4 字节长度前缀 + JSON」TCP 帧互转。

## 目录结构

```
web/
  package.json        依赖与脚本
  vite.config.ts      开发服务器 + /ws 代理
  tsconfig.json       TypeScript 配置
  index.html
  src/
    main.tsx          入口，加载 TDesign 样式
    App.tsx           登录 / 主界面切换
    types.ts          协议类型
    net/transport.ts  WebSocket 传输层（自动重连 + 心跳）
    store/chatStore.ts 全局状态 + 协议分发 + 动作
    utils/util.ts     头像配色 / 时间格式化
    components/
      Login.tsx       登录页（登录即注册）
      MainLayout.tsx  侧栏 + 聊天区
      ConversationList.tsx  会话列表（搜索 / 未读红点 / 在线点 / 右键菜单）
      ChatPanel.tsx   聊天面板（头部 / 消息列表 / 输入）
      MessageBubble.tsx 气泡（右键复制 / 撤回 / 删除）
      Dialogs.tsx     新建群聊 / 发现群聊 / 成员列表
    styles/app.css    浅色主题样式
```

## 运行

### 1. 启动 Python 服务器（已有）

```bash
python server.py            # 默认监听 0.0.0.0:9000
```

### 2. 启动 WebSocket 桥接（浏览器经它连 TCP 服务器）

```bash
pip install websockets
python ws_bridge.py
# 默认监听 ws://127.0.0.1:9106/ws，转发到 127.0.0.1:9000
```

环境变量：`MOYU_BACKEND_HOST/PORT`（TCP 后端，默认 `127.0.0.1:9000`，与服务器 `common.PORT` 一致）、`MOYU_WS_HOST/PORT`（桥接监听，默认 `0.0.0.0:9106`）。

### 3. 启动前端（需要 Node ≥ 18）

```bash
cd web
npm install
npm run dev        # 开发：http://localhost:5173，/ws 由 Vite 代理到桥接
# 或 npm run build && npm run preview
```

浏览器访问 `http://localhost:5173`，输入昵称即可开聊。生产部署时把 `/ws` 指向桥接服务
（或设置 `VITE_WS_URL`，如 `wss://chat.example.com/ws`）。

> 无浏览器环境下的运行时验证：`npm run smoke` 会用 jsdom 挂载真实 React 应用并喂入
> 协议消息，校验登录页 / 会话列表 / 气泡 / 输入框能无异常渲染。

## 已实现

- 登录即注册、自动重连、应用层心跳（ping/pong 超时判死链）
- 会话列表：搜索、未读红点、在线状态点、置顶、右键删除/退群
- 聊天气泡：左右对齐、群昵称着色、日期分隔条、单勾(✓)/双勾(✓✓)已读
- 群聊：建群、邀请成员、发现群聊、查看成员、退群
- 消息：发送（Enter 发送 / Shift+Enter 换行）、撤回、本端删除、复制
- 实时：新消息、已读回执、正在输入、上下线、撤回广播

## 与 Tkinter 桌面端（client.py）的关系

`client.py` 是旧版桌面客户端，仍可直接 `python client.py` 使用；本目录是响应式网页版，
二者共用同一套服务器与协议。
