// 运行时冒烟渲染：用 jsdom 挂载真实 React 应用，喂入真实协议消息，
// 验证登录页 / 会话列表 / 聊天气泡 / 输入框能在无浏览器环境下无异常渲染。
// 运行：node smoke/smoke.mjs（先由 esbuild 打包）。

import { JSDOM } from 'jsdom'

const errors: string[] = []
function fail(msg: string) { errors.push(msg) }

// --- 构造浏览器全局环境（必须在 import React/App 之前设置） ---
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: 'http://localhost:5173/',
  pretendToBeVisual: true,
})
const w: any = dom.window
w.matchMedia = w.matchMedia || (() => ({
  matches: false, media: '', onchange: null,
  addListener() {}, removeListener() {},
  addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false },
}))
class RO { observe() {} unobserve() {} disconnect() {} }
class IO { observe() {} unobserve() {} disconnect() {} takeRecords() { return [] } }
function def(name: string, value: any) {
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true })
}
def('window', w)
def('document', w.document)
def('navigator', w.navigator)
def('location', w.location)
def('matchMedia', w.matchMedia)
def('ResizeObserver', RO)
def('IntersectionObserver', IO)
def('requestAnimationFrame', (cb: any) => setTimeout(cb, 0))
def('cancelAnimationFrame', (id: any) => clearTimeout(id))
// TDesign / react-dom 会对元素做 instanceof 判断，需把 jsdom 的构造器挂到全局
for (const k of ['Element', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement',
  'Node', 'NodeList', 'SVGElement', 'DocumentFragment', 'Document', 'Event',
  'MouseEvent', 'KeyboardEvent', 'CustomEvent', 'PointerEvent', 'FocusEvent',
  'MutationObserver', 'DOMParser']) {
  if ((w as any)[k]) def(k, (w as any)[k])
}
def('getComputedStyle', w.getComputedStyle.bind(w))
def('requestIdleCallback', (cb: any) => setTimeout(cb, 0))
def('cancelIdleCallback', (id: any) => clearTimeout(id))
// 屏蔽真实 WebSocket，避免重连/网络；transport 的 new WebSocket 由 try/catch 兜住
globalThis.WebSocket = class {
  static OPEN = 1
  readyState = 0
  send() {}
  close() {}
  addEventListener() {}
  removeEventListener() {}
} as any

const origError = console.error
console.error = (...a: any[]) => { errors.push('console.error: ' + a.map(String).join(' ')) }

const tick = () => new Promise((r) => setTimeout(r, 60))

async function main() {
  const React = (await import('react')).default
  const { createRoot } = await import('react-dom/client')
  const App = (await import('../src/App')).default
  const store = await import('../src/store/chatStore')

  const root = createRoot(document.getElementById('root')!)
  root.render(React.createElement(App))
  await tick()

  const body = () => document.body.innerHTML

  // 1) 登录页
  if (!body().includes('MoyuChat')) fail('登录页未渲染出标题 MoyuChat')
  if (!body().includes('输入昵称')) fail('登录页未渲染出昵称输入占位')
  origError('PASS 登录页渲染')

  // 2) 模拟登录与协议消息
  store.login('alice')
  store.dispatch({ type: 'login_ok' })
  store.dispatch({ type: 'user_list', users: ['alice', 'bob', 'carol'] })
  store.dispatch({ type: 'group_list', groups: ['dev'] })
  store.dispatch({ type: 'group_info', group: 'dev', members: ['alice', 'bob'] })
  const ts = Math.floor(Date.now() / 1000)
  store.dispatch({ type: 'private', id: 1, from: 'bob', to: 'alice', text: '你好，这是测试消息', ts })
  store.dispatch({ type: 'group', id: 2, group: 'dev', from: 'bob', text: '群里大家好', ts })
  await tick()

  // 3) 会话列表
  if (!body().includes('bob')) fail('会话列表未显示 bob')
  if (!body().includes('dev')) fail('会话列表未显示群 dev')
  if (!body().includes('群里大家好')) fail('会话预览未显示群消息')
  origError('PASS 会话列表渲染')

  // 4) 打开私聊 → 聊天气泡 + 输入框
  store.selectConv('bob')
  await tick()
  if (!body().includes('你好，这是测试消息')) fail('聊天区未显示私聊消息气泡')
  if (!body().includes('消息')) fail('聊天区未渲染输入框占位')
  origError('PASS 私聊气泡 + 输入框渲染')

  // 5) 打开群聊 → 群昵称 + 气泡
  store.selectConv('dev')
  await tick()
  if (!body().includes('群里大家好')) fail('群聊区未显示群消息')
  if (!body().includes('群聊')) fail('群聊头部未显示“群聊”')
  origError('PASS 群聊气泡 + 头部渲染')

  // 6) 输入一条消息 → 乐观气泡立即出现
  store.sendMessage('我发出的消息')
  await tick()
  if (!body().includes('我发出的消息')) fail('发送后未出现乐观气泡')
  origError('PASS 发送乐观气泡渲染')

  root.unmount()
  console.error = origError
  if (errors.length) {
    origError('\nFAIL smoke render:')
    for (const e of errors) origError('  - ' + e)
    process.exit(1)
  }
  origError('\nPASS jsdom smoke render all')
  process.exit(0)
}

main().catch((e) => {
  origError('FAIL smoke render exception:', e)
  process.exit(1)
})
