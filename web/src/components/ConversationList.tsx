import { useState, useEffect, useRef } from 'react'
import { Input, Dialog } from 'tdesign-react'
import { useChat, selectConv, setSearch, togglePin, deleteConversation,
  leaveGroup, queryUser, startChat } from '../store/chatStore'
import { colorOf, initialOf, formatPreview, formatClock } from '../utils/util'
import type { Conversation } from '../types'

interface MenuState {
  x: number
  y: number
  conv: Conversation
}

export default function ConversationList() {
  const s = useChat()
  const [menu, setMenu] = useState<MenuState | null>(null)
  const [showResults, setShowResults] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // 点击别处关闭右键菜单
  useEffect(() => {
    const close = () => setMenu(null)
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [])

  const term = s.searchTerm.trim().toLowerCase()
  const list = s.conversations.filter((c) => {
    if (!term) return true
    if (c.name.toLowerCase().includes(term)) return true
    return (s.messages[c.name] || []).some((m) => m.text.toLowerCase().includes(term))
  })

  const openMenu = (e: React.MouseEvent, conv: Conversation) => {
    e.preventDefault()
    e.stopPropagation()
    setMenu({ x: e.clientX, y: e.clientY, conv })
  }

  return (
    <div className="sidebar" ref={ref}>
      <div className="sidebar-head">
        <div className="me-name">{s.username}</div>
        <Input
          value={s.searchTerm}
          onChange={(v) => setSearch(v as string)}
          onEnter={(v) => { queryUser(v as string); setShowResults(true) }}
          placeholder="搜索（回车可跨服务器查找用户）"
          size="small"
          className="search-input"
        />
        <Dialog
          header="发现结果"
          visible={showResults}
          onClose={() => setShowResults(false)}
          footer={false}
        >
          {s.searchResults.map((u) => (
            <div key={u.name} className="search-result-item">
              <span>{u.name}　·　{u.remote ? `异地（服务器 ${u.server ?? '?'}）` : '本服务器'}</span>
              <button className="join-btn" onClick={() => { startChat(u.name); setShowResults(false) }}>
                聊天
              </button>
            </div>
          ))}
          {s.searchResults.length === 0 && <div className="conv-empty">未找到该用户</div>}
        </Dialog>
      </div>
      <div className="conv-list">
        {list.length === 0 && <div className="conv-empty">没有匹配的会话</div>}
        {list.map((c) => (
          <div
            key={c.name}
            className={'conv-item' + (c.name === s.active ? ' active' : '')}
            onClick={() => selectConv(c.name)}
            onContextMenu={(e) => openMenu(e, c)}
          >
            <div className="conv-avatar" style={{ background: colorOf(c.name) }}>
              {initialOf(c.name)}
              {c.type === 'user' && (
                <span
                  className={'presence-dot' + (c.presence ? ' on' : '')}
                />
              )}
            </div>
            <div className="conv-main">
              <div className="conv-top">
                <span className="conv-name">
                  {c.name}
                  {c.pinned && <span className="pin-tag">置顶</span>}
                </span>
                <span className="conv-time">
                  {c.last ? formatClock(c.last.ts) : ''}
                </span>
              </div>
              <div className="conv-bottom">
                <span className="conv-preview">
                  {c.type === 'group' ? `群 · ${(c.members || []).length} 人` : ''}
                  {(c.type === 'user' ? previewText(s, c) : (c.last?.system ? c.last.text : c.last?.text)) || ''}
                </span>
                {c.unread > 0 && (
                  <span className="unread-badge">
                    {c.unread > 99 ? '99+' : c.unread}
                  </span>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>

      {menu && (
        <div
          className="context-menu"
          style={{ left: menu.x, top: menu.y }}
          onClick={(e) => e.stopPropagation()}
        >
          <div
            className="context-item"
            onClick={() => {
              togglePin(menu.conv.name)
              setMenu(null)
            }}
          >
            {menu.conv.pinned ? '取消置顶' : '置顶'}
          </div>
          {menu.conv.type === 'group' && (
            <div
              className="context-item"
              onClick={() => {
                leaveGroup(menu.conv.name)
                setMenu(null)
              }}
            >
              退出群组
            </div>
          )}
          <div
            className="context-item danger"
            onClick={() => {
              deleteConversation(menu.conv.name)
              setMenu(null)
            }}
          >
            删除会话
          </div>
        </div>
      )}
    </div>
  )
}

function previewText(s: ReturnType<typeof useChat>, c: Conversation): string {
  const last = c.last
  if (!last) return ''
  if (last.recalled) return '消息已撤回'
  if (last.system) return last.text
  const prefix = last.from && last.from !== s.username ? last.from + '：' : ''
  return prefix + last.text
}
