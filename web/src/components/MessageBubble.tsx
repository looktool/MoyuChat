import { useState } from 'react'
import { MessagePlugin } from 'tdesign-react'
import type { ConvType, Message } from '../types'
import { colorOf, initialOf, formatClock } from '../utils/util'
import { recallMessage, deleteLocalMessage } from '../store/chatStore'

interface Props {
  m: Message
  type: ConvType
  me: string
}

export default function MessageBubble({ m, type, me }: Props) {
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null)

  if (m.system) {
    return <div className="sys-msg">{m.text}</div>
  }
  if (m.recalled) {
    return <div className="sys-msg">消息已撤回</div>
  }

  const sent = m.from === me
  const showAvatar = type === 'group' && !sent

  const copy = () => {
    navigator.clipboard?.writeText(m.text)
    MessagePlugin.success('已复制')
    setMenu(null)
  }
  const recall = () => {
    recallMessage(m)
    setMenu(null)
  }
  const del = () => {
    deleteLocalMessage(m)
    setMenu(null)
  }

  return (
    <div
      className={'bubble-row' + (sent ? ' sent' : ' recv')}
      onContextMenu={(e) => {
        e.preventDefault()
        setMenu({ x: e.clientX, y: e.clientY })
      }}
    >
      {showAvatar && (
        <div className="bubble-avatar" style={{ background: colorOf(m.from) }}>
          {initialOf(m.from)}
        </div>
      )}
      <div className="bubble-wrap">
        {showAvatar && (
          <div className="bubble-name" style={{ color: colorOf(m.from) }}>
            {m.from}
          </div>
        )}
        <div className={'bubble' + (sent ? ' me' : '')}>
          <div className="bubble-text">{m.text}</div>
          <div className="bubble-meta">
            {formatClock(m.ts)}
            {sent && (
              <span className={'tick' + (m.status === 'read' ? ' read' : '')}>
                {m.status === 'read' ? '✓✓' : '✓'}
              </span>
            )}
          </div>
        </div>
      </div>

      {menu && (
        <div
          className="context-menu"
          style={{ left: menu.x, top: menu.y }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="context-item" onClick={copy}>复制</div>
          {sent && (
            <div className="context-item" onClick={recall}>撤回</div>
          )}
          <div className="context-item danger" onClick={del}>删除（仅本端）</div>
        </div>
      )}
    </div>
  )
}
