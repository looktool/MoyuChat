// 头像配色与文字工具：从名字派生稳定颜色（与 Tkinter 端风格一致）。

const COLORS = [
  '#5b8def', '#37c091', '#f2a23c', '#9b7bf0',
  '#ef6f9e', '#46c0d4', '#e8743b', '#4aa3df',
]

export function colorOf(name: string): string {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return COLORS[h % COLORS.length]
}

export function initialOf(name: string): string {
  const s = (name || '').trim()
  return s ? s[0].toUpperCase() : '?'
}

// ---- 时间格式化 ----

function pad(n: number): string {
  return n < 10 ? '0' + n : '' + n
}

export function formatClock(ts: number): string {
  const d = new Date(ts * 1000)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function dayKey(ts: number): string {
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

export function dayLabel(ts: number): string {
  const key = dayKey(ts)
  const today = dayKey(Date.now() / 1000)
  const yesterday = dayKey(Date.now() / 1000 - 86400)
  if (key === today) return '今天'
  if (key === yesterday) return '昨天'
  return key
}

export function formatPreview(ts: number): string {
  return formatClock(ts)
}
