import { useState, useEffect, useRef } from 'react'
import { Button, Dropdown } from 'tdesign-react'
import { useChat, sendMessage, selectConv } from '../store/chatStore'
import * as transport from '../net/transport'
import { colorOf, initialOf, dayLabel, dayKey } from '../utils/util'
import MessageBubble from './MessageBubble'
import { NewGroupDialog, DiscoverDialog, MemberDialog } from './Dialogs'

export default function ChatPanel() {
  const s = useChat()
  const conv = s.active!
  const c = s.conversations.find((x) => x.name === conv)
  const type = s.activeType!
  const msgs = s.messages[conv] || []
  const scrollRef = useRef<HTMLDivElement>(null)
  const [newGroup, setNewGroup] = useState(false)
  const [discover, setDiscover] = useState(false)
  const [members, setMembers] = useState(false)
  const typing = s.typing[conv]

  // 新消息到来时滚到底部
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [msgs.length, conv])

  const [draft, setDraft] = useState('')
  const draftRef = useRef('')
  draftRef.current = draft

  const send = () => {
    const text = draftRef.current.trim()
    if (!text) return
    sendMessage(text)
    draftRef.current = ''
    setDraft('')
  }

  const onInput = (v: string) => {
    setDraft(v)
    if (v.trim()) {
      const payload: any = { type: 'typing', conv }
      if (type === 'group') payload.group = conv
      transport.send(payload)
    }
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  const subtitle = c?.type === 'group'
    ? `群聊 · ${(c.members || []).length} 人`
    : c?.presence ? '在线' : '离线'

  const moreOptions = [
    { content: '新建群聊', value: 'new' },
    { content: '发现群聊', value: 'discover' },
    ...(type === 'group' ? [{ content: '查看成员', value: 'members' }] : []),
  ]

  return (
    <div className="chat">
      <div className="chat-head">
        <div className="chat-head-avatar" style={{ background: colorOf(conv) }}>
          {initialOf(conv)}
        </div>
        <div className="chat-head-info">
          <div className="chat-head-name">{conv}</div>
          <div className="chat-head-sub">
            {typing ? '对方正在输入…' : subtitle}
          </div>
        </div>
        <Dropdown
          options={moreOptions}
          onClick={(item) => {
            const value = (item as any)?.value
            if (value === 'new') setNewGroup(true)
            else if (value === 'discover') setDiscover(true)
            else if (value === 'members') setMembers(true)
          }}
        >
          <Button variant="text" theme="primary">⋯</Button>
        </Dropdown>
      </div>

      <div className="chat-body" ref={scrollRef}>
        {msgs.map((m, i) => {
          const prev = msgs[i - 1]
          const showDay = !prev || dayKey(prev.ts) !== dayKey(m.ts)
          return (
            <div key={m.id ?? `${m.ts}-${i}`}>
              {showDay && <div className="date-chip">{dayLabel(m.ts)}</div>}
              <MessageBubble m={m} type={type} me={s.username} />
            </div>
          )
        })}
      </div>

      <div className="chat-input">
        <textarea
          className="chat-textarea"
          value={draft}
          onChange={(e) => onInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="消息"
          rows={1}
        />
        <Button theme="primary" shape="circle" onClick={send} className="send-btn">
          ➤
        </Button>
      </div>

      {newGroup && <NewGroupDialog onClose={() => setNewGroup(false)} />}
      {discover && <DiscoverDialog onClose={() => setDiscover(false)} />}
      {members && c?.type === 'group' && (
        <MemberDialog name={conv} members={c.members || []} onClose={() => setMembers(false)} />
      )}
    </div>
  )
}
