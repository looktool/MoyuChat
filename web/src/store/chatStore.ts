// 全局状态：以 useSyncExternalStore 暴露给 React 组件。
// 负责：解析服务器协议、维护会话/消息/在线/输入状态、发起客户端动作。

import { useSyncExternalStore } from 'react'
import * as transport from '../net/transport'
import type {
  ConnStatus, ConvType, Conversation, GroupInfo, Message, ServerMsg,
} from '../types'

interface State {
  status: ConnStatus
  username: string
  error?: string
  conversations: Conversation[]
  messages: Record<string, Message[]>
  presence: Record<string, boolean>
  typing: Record<string, boolean>
  groups: GroupInfo[]
  allGroups: GroupInfo[]
  active: string | null
  activeType: ConvType | null
  searchTerm: string
  searchResults: FoundUser[]   // 跨服务器发现结果
}

export interface FoundUser {
  name: string
  remote: boolean   // true=在另一台对等服务器上（跨服务器）
  server?: string
}

let state: State = {
  status: 'disconnected',
  username: '',
  conversations: [],
  messages: {},
  presence: {},
  typing: {},
  groups: [],
  allGroups: [],
  active: null,
  activeType: null,
  searchTerm: '',
  searchResults: [],
}

const listeners = new Set<() => void>()
function emit() {
  for (const l of listeners) l()
}
function subscribe(l: () => void) {
  listeners.add(l)
  return () => listeners.delete(l)
}
function getState(): State {
  return state
}
function setState(patch: Partial<State>) {
  state = { ...state, ...patch }
  emit()
}

// 协议分发入口（transport 收到非握手类消息时调用）。字段形状严格对齐 server.py。
export function dispatch(msg: ServerMsg) {
  switch (msg.type) {
    case 'login_ok': return handleLoginOk(msg)
    case 'login_fail':
      setState({ status: 'disconnected', error: msg.reason || '登录失败' })
      return
    case 'user_list': return handleUserList(msg)
    case 'group_list': return handleGroupList(msg)
    case 'all_groups': return handleAllGroups(msg)
    case 'private': return handlePrivate(msg)
    case 'group': return handleGroup(msg)
    case 'read_receipt': return handleReadReceipt(msg)
    case 'typing': return handleTyping(msg)
    case 'presence': return handlePresence(msg)
    case 'recalled': return handleRecalled(msg)
    case 'group_info': return handleGroupInfo(msg)
    case 'left_group': return handleLeftGroup(msg)
    case 'system': return handleSystem(msg)
    case 'search_result': return handleSearchResult(msg)
    default: return
  }
}

// 跨服务器发现：本服务器 + 所有已连接的对等服务器上的同名用户
function handleSearchResult(msg: any) {
  const users: FoundUser[] = Array.isArray(msg.users) ? msg.users : []
  setState({ searchResults: users })
}

function normalize(h: any, username: string): Message | null {
  if (!h) return null
  if (h.type === 'group' || h.group) {
    return {
      conv: h.group, from: h.from, group: h.group, text: h.text, ts: h.ts,
      system: h.system, recalled: h.recalled, id: h.id, replay: h.replay,
      status: h.from === username ? 'sent' : undefined,
    }
  }
  const fromMe = h.from === username
  return {
    conv: fromMe ? h.to : h.from, from: h.from, to: h.to, text: h.text, ts: h.ts,
    system: h.system, recalled: h.recalled, id: h.id, replay: h.replay,
    status: fromMe ? 'sent' : undefined,
  }
}

function reorder(list: Conversation[]): Conversation[] {
  return [...list].sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1
    const ta = a.last?.ts ?? 0
    const tb = b.last?.ts ?? 0
    return tb - ta
  })
}

function peerOf(conv: string): string {
  const c = state.conversations.find((x) => x.name === conv)
  return c?.type === 'user' ? conv : ''
}

// login_ok 只表示登录成功（含心跳启动）；会话/历史由随后的 user_list /
// group_list / private(回放) / group(回放) 逐步补齐，因此这里只切状态、清错误。
function handleLoginOk(_msg: any) {
  setState({ status: 'online', error: undefined })
}

// user_list: {users:[在线用户名...]}（含自己）。先全量置离线再点亮在线，避免重连后残留。
function handleUserList(msg: any) {
  const users = (msg.users || []).filter(
    (u: any) => typeof u === 'string' && u !== state.username)
  const presence: Record<string, boolean> = { ...state.presence }
  for (const k of Object.keys(presence)) {
    if (k !== state.username) presence[k] = false
  }
  const conversations = [...state.conversations]
  for (const u of users) {
    presence[u] = true
    if (!conversations.find((c) => c.name === u)) {
      conversations.push({ name: u, type: 'user', unread: 0, pinned: false })
    }
  }
  presence[state.username] = true
  setState({ presence, conversations: reorder(conversations) })
}

// group_list: {groups:[我加入的群名...]}（字符串列表，非对象）。
function handleGroupList(msg: any) {
  const names = (msg.groups || []).filter(
    (g: any) => typeof g === 'string' && g)
  const conversations = [...state.conversations]
  for (const g of names) {
    if (!conversations.find((c) => c.name === g)) {
      conversations.push({ name: g, type: 'group', unread: 0, pinned: false })
    }
  }
  setState({
    conversations: reorder(conversations),
    groups: names.map((n: string) => ({ name: n, member_count: 0, joined: true })),
  })
}

// all_groups（发现群聊）: {groups:[{name,count,joined}]}
function handleAllGroups(msg: any) {
  const groups: GroupInfo[] = (msg.groups || [])
    .filter((g: any) => g && typeof g.name === 'string')
    .map((g: any) => ({
      name: g.name, member_count: g.count || 0, joined: !!g.joined,
    }))
  setState({ allGroups: groups })
}

// 被踢出群：移除会话与历史
function handleLeftGroup(msg: any) {
  const g = msg.group
  if (!g) return
  const messages = { ...state.messages }
  delete messages[g]
  setState({
    messages,
    conversations: state.conversations.filter((c) => c.name !== g),
    active: state.active === g ? null : state.active,
  })
}

function upsertMessage(conv: string, type: ConvType, m: Message, incoming: boolean) {
  const list = state.messages[conv] ? [...state.messages[conv]] : []
  let isNew = true
  if (m.id !== undefined && m.id !== null && m.id !== "") {
    // 服务器消息带真实 id：优先按 id 去重（重连/补投的历史不会重复显示）
    const idx = list.findIndex((x) => x.id === m.id)
    if (idx >= 0) {
      list[idx] = m
      isNew = false
    } else {
      // 再用“同文本、本人发出”的本地乐观消息（负 id）替换，避免出现双气泡
      const oi = list.findIndex(
        (x) => typeof x.id === 'number' && x.id < 0
          && x.from === state.username && x.text === m.text)
      if (oi >= 0) {
        list[oi] = m
        isNew = false
      } else {
        list.push(m)
      }
    }
  } else if (m.id !== undefined && typeof m.id === 'number' && m.id < 0 && m.from === state.username) {
    // 本地乐观消息：用真实回显（同文本）替换，避免双气泡
    const idx = list.findIndex((x) => typeof x.id === 'number' && x.id < 0 && x.text === m.text)
    if (idx >= 0) {
      list[idx] = m
      isNew = false
    } else {
      list.push(m)
    }
  } else {
    list.push(m)
  }
  const messages = { ...state.messages, [conv]: list }
  const conversations = [...state.conversations]
  let c = conversations.find((x) => x.name === conv)
  if (!c) {
    c = { name: conv, type, unread: 0, pinned: false }
    conversations.push(c)
  }
  c = { ...c, last: m }
  if (isNew && incoming && conv !== state.active) {
    c = { ...c, unread: (c.unread || 0) + 1 }
  }
  const ci = conversations.findIndex((x) => x.name === conv)
  conversations[ci] = c
  setState({ messages, conversations: reorder(conversations) })
  if (incoming && conv === state.active) markRead(conv)
}

function handlePrivate(msg: any) {
  const fromMe = msg.from === state.username
  const conv = fromMe ? msg.to : msg.from
  const m = normalize(msg, state.username)
  if (m) upsertMessage(conv, 'user', m, !fromMe)
}

function handleGroup(msg: any) {
  const m = normalize(msg, state.username)
  if (m && m.conv) upsertMessage(m.conv, 'group', m, msg.from !== state.username)
}

function handleReadReceipt(msg: any) {
  const conv = msg.target
  const list = state.messages[conv]
  if (!list) return
  let changed = false
  const next = list.map((m) => {
    if (m.id === msg.id && m.from === state.username) {
      changed = true
      return { ...m, status: 'read' as const }
    }
    return m
  })
  if (changed) setState({ messages: { ...state.messages, [conv]: next } })
}

function handleTyping(msg: any) {
  const conv = msg.conv
  if (!conv) return
  setState({ typing: { ...state.typing, [conv]: true } })
  setTimeout(() => {
    if (state.typing[conv]) {
      setState({ typing: { ...state.typing, [conv]: false } })
    }
  }, 4000)
}

function handlePresence(msg: any) {
  const presence = { ...state.presence, [msg.user]: !!msg.online }
  const conversations = state.conversations.map((c) =>
    c.type === 'user' && c.name === msg.user ? { ...c, presence: !!msg.online } : c)
  setState({ presence, conversations })
}

function handleRecalled(msg: any) {
  const conv = msg.conv
  const list = state.messages[conv]
  if (!list) return
  let changed = false
  const next = list.map((m) => {
    if (m.id === msg.id) {
      changed = true
      return { ...m, recalled: true, text: '' }
    }
    return m
  })
  if (changed) {
    const conversations = state.conversations.map((c) => {
      if (c.name === conv && c.last && c.last.id === msg.id) {
        return { ...c, last: { ...c.last, recalled: true, text: '' } }
      }
      return c
    })
    setState({ messages: { ...state.messages, [conv]: next }, conversations })
  }
}

function handleGroupInfo(msg: any) {
  const conversations = [...state.conversations]
  let c = conversations.find((x) => x.name === msg.group)
  if (!c) {
    c = { name: msg.group, type: 'group', unread: 0, pinned: false }
    conversations.push(c)
  }
  c = { ...c, members: msg.members || c.members }
  const ci = conversations.findIndex((x) => x.name === msg.group)
  conversations[ci] = c
  const groups = state.groups.map((g) => g.name === msg.group ? { ...g, joined: true } : g)
  const allGroups = state.allGroups.map((g) => g.name === msg.group ? { ...g, joined: true } : g)
  setState({ conversations: reorder(conversations), groups, allGroups })
}

function handleSystem(msg: any) {
  if (!msg.conv) return
  const c = state.conversations.find((x) => x.name === msg.conv)
  upsertMessage(msg.conv, c?.type || 'user', {
    conv: msg.conv, from: '', text: msg.text, ts: msg.ts || Date.now() / 1000,
    system: true,
  }, false)
}

export function markRead(conv: string) {
  const list = state.messages[conv] || []
  const pending = list.filter(
    (m) => !m.system && m.from !== state.username && m.id && m.status !== 'read')
  if (!pending.length) {
    const c = state.conversations.find((x) => x.name === conv)
    if (c && c.unread) {
      setState({ conversations: state.conversations.map((x) => x.name === conv ? { ...x, unread: 0 } : x) })
    }
    return
  }
  const ids = pending.map((m) => m.id!)
  const messages = { ...state.messages, [conv]: list.map((m) =>
    m.from !== state.username && !m.system && m.id && ids.includes(m.id)
      ? { ...m, status: 'read' as const } : m) }
  setState({
    messages,
    conversations: state.conversations.map((x) => x.name === conv ? { ...x, unread: 0 } : x),
  })
  const payload: any = { type: 'read', target: conv, ids }
  const peer = peerOf(conv)
  if (peer) payload.to = peer
  transport.send(payload)
}

// ---- 客户端动作 ----

export function login(username: string) {
  setState({ username, status: 'connecting', error: undefined })
  transport.connect(username)
}

export function sendMessage(text: string) {
  const conv = state.active
  const type = state.activeType
  const body = (text || '').trim()
  if (!conv || !body) return
  const username = state.username
  const ts = Date.now() / 1000
  const optimistic: Message = {
    conv, from: username, text: body, ts, status: 'sent', id: -Date.now(),
  }
  if (type === 'group') optimistic.group = conv
  else optimistic.to = conv
  const messages = { ...state.messages, [conv]: [...(state.messages[conv] || []), optimistic] }
  const conversations = state.conversations.map((c) => c.name === conv ? { ...c, last: optimistic } : c)
  setState({ messages, conversations: reorder(conversations) })
  if (type === 'group') transport.send({ type: 'group_msg', group: conv, text: body })
  else transport.send({ type: 'chat', to: conv, text: body })
}

export function selectConv(name: string) {
  const c = state.conversations.find((x) => x.name === name)
  if (!c) return
  setState({ active: name, activeType: c.type })
  markRead(name)
}

export function setSearch(term: string) {
  setState({ searchTerm: term })
}

// 跨服务器发现：向服务器查询某用户名（本服务器没有也会问所有已连接的对等服务器）
export function queryUser(name: string) {
  const n = (name || '').trim()
  if (n) transport.send({ type: 'query_user', user: n })
}

// 与发现的用户开聊：确保存在该会话并切到它（对方在另一台服务器也能直接发消息，
// 由服务器经联邦转发）。
export function startChat(name: string) {
  const conversations = [...state.conversations]
  if (!conversations.find((x) => x.name === name)) {
    conversations.push({ name, type: 'user', unread: 0, pinned: false })
  }
  setState({ conversations: reorder(conversations), active: name, activeType: 'user' })
  markRead(name)
}

export function createGroup(name: string, members: string[]) {
  transport.send({ type: 'group_create', group: name, members })
}

export function joinGroup(name: string) {
  transport.send({ type: 'group_join', group: name })
}

export function inviteMember(name: string, members: string[]) {
  // 对已存在群，group_create 即“邀请成员”
  transport.send({ type: 'group_create', group: name, members })
}

export function recallMessage(m: Message) {
  if (m.id === undefined || (typeof m.id === 'number' && m.id < 0)) return
  const payload: any = { type: 'recalled', id: m.id, conv: m.conv }
  if (m.group) payload.group = m.group
  else payload.to = peerOf(m.conv)
  transport.send(payload)
  // 本地即时反映撤回状态
  const list = state.messages[m.conv] || []
  setState({
    messages: { ...state.messages, [m.conv]: list.map((x) =>
      x.id === m.id ? { ...x, recalled: true, text: '' } : x) },
  })
}

export function deleteLocalMessage(m: Message) {
  const list = (state.messages[m.conv] || []).filter((x) => x.id !== m.id)
  setState({ messages: { ...state.messages, [m.conv]: list } })
}

export function togglePin(name: string) {
  setState({
    conversations: reorder(state.conversations.map((c) =>
      c.name === name ? { ...c, pinned: !c.pinned } : c)),
  })
}

export function deleteConversation(name: string) {
  const messages = { ...state.messages }
  delete messages[name]
  setState({
    conversations: state.conversations.filter((c) => c.name !== name),
    messages,
    active: state.active === name ? null : state.active,
  })
}

export function leaveGroup(name: string) {
  transport.send({ type: 'group_leave', group: name })
  const messages = { ...state.messages }
  delete messages[name]
  setState({
    conversations: state.conversations.filter((c) => c.name !== name),
    messages,
    active: state.active === name ? null : state.active,
  })
}

export function knownUsers(): string[] {
  const set = new Set<string>()
  for (const k of Object.keys(state.presence)) set.add(k)
  for (const c of state.conversations) if (c.type === 'user') set.add(c.name)
  set.delete(state.username)
  return Array.from(set)
}

// 订阅钩子
export function useChat(): State {
  return useSyncExternalStore(subscribe, getState, getState)
}

// 初始化：把 transport 的回调绑定到 store
transport.onMessage(dispatch)
transport.onStateChange((s) => setState({ status: s }))
