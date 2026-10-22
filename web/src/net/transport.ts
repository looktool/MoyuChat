// WebSocket 传输层：负责连接、自动重连、心跳，以及把服务器消息分发到 store。
// 浏览器不能直连 TCP，所以这里连的是 ws_bridge.py（默认 /ws，开发期经 Vite 代理）。

import type { ConnStatus, ServerMsg, ClientMsg } from '../types'

type MsgHandler = (msg: ServerMsg) => void
type StateHandler = (s: ConnStatus) => void

let ws: WebSocket | null = null
let username = ''
let shouldReconnect = true
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let heartbeat: ReturnType<typeof setInterval> | null = null
let lastPong = 0

export let connectionState: ConnStatus = 'disconnected'

let msgHandler: MsgHandler = () => {}
let stateHandler: StateHandler = () => {}

const WS_URL =
  (import.meta as any).env?.VITE_WS_URL ||
  `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`

function setState(s: ConnStatus) {
  connectionState = s
  stateHandler(s)
}

export function onMessage(h: MsgHandler) {
  msgHandler = h
}
export function onStateChange(h: StateHandler) {
  stateHandler = h
}

export function connect(user: string) {
  username = user
  shouldReconnect = true
  open()
}

export function close() {
  shouldReconnect = false
  if (heartbeat) clearInterval(heartbeat)
  heartbeat = null
  ws?.close()
}

function open() {
  setState('connecting')
  let sock: WebSocket
  try {
    sock = new WebSocket(WS_URL)
  } catch (e) {
    scheduleReconnect()
    return
  }
  ws = sock
  sock.onopen = () => {
    lastPong = Date.now()
    sock.send(JSON.stringify({ type: 'login', username }))
    // 心跳：每 30s ping 一次，超过 100s 没收到 pong 则判定链路已死并触发重连
    if (heartbeat) clearInterval(heartbeat)
    heartbeat = setInterval(() => {
      if (Date.now() - lastPong > 100_000) {
        sock.close()
        return
      }
      send({ type: 'ping', ts: Date.now() / 1000 })
    }, 30_000)
  }
  sock.onmessage = (ev) => {
    let msg: ServerMsg
    try {
      msg = JSON.parse(ev.data as string)
    } catch {
      return
    }
    if (msg.type === 'pong') {
      lastPong = Date.now()
      return
    }
    if (msg.type === 'kicked') {
      shouldReconnect = false
      if (heartbeat) clearInterval(heartbeat)
      setState('kicked')
      return
    }
    if (msg.type === 'login_fail') {
      // 用户名非法：停止重连，避免反复登录失败死循环
      shouldReconnect = false
      setState('disconnected')
      msgHandler(msg)
      return
    }
    if (msg.type === 'login_ok') {
      lastPong = Date.now()
      setState('online')
      return
    }
    msgHandler(msg)
  }
  sock.onclose = () => {
    if (heartbeat) clearInterval(heartbeat)
    heartbeat = null
    setState('disconnected')
    if (shouldReconnect && username) scheduleReconnect()
  }
  sock.onerror = () => {
    sock.close()
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    if (shouldReconnect && username) open()
  }, 1500)
}

export function send(msg: ClientMsg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg))
  }
}
