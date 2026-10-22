import { useState } from 'react'
import { Dialog, Input, Button, MessagePlugin } from 'tdesign-react'
import { useChat, createGroup, joinGroup, knownUsers } from '../store/chatStore'
import { colorOf, initialOf } from '../utils/util'

export function NewGroupDialog({ onClose }: { onClose: () => void }) {
  const s = useChat()
  const candidates = knownUsers()
  const [name, setName] = useState('')
  const [sel, setSel] = useState<Set<string>>(new Set())

  const toggle = (u: string) => {
    const n = new Set(sel)
    if (n.has(u)) n.delete(u)
    else n.add(u)
    setSel(n)
  }

  const confirm = () => {
    const v = name.trim()
    if (!v) {
      MessagePlugin.warning('请输入群名称')
      return
    }
    createGroup(v, Array.from(sel))
    onClose()
  }

  return (
    <Dialog
      visible
      header="新建群聊"
      onClose={onClose}
      onConfirm={confirm}
      confirmBtn="创建"
      cancelBtn="取消"
    >
      <Input value={name} onChange={(v) => setName(v as string)} placeholder="群名称" />
      <div className="member-pick">
        {candidates.length === 0 && <div className="conv-empty">暂无可邀请的人</div>}
        {candidates.map((u) => (
          <label key={u} className={'pick-item' + (sel.has(u) ? ' on' : '')}>
            <input type="checkbox" checked={sel.has(u)} onChange={() => toggle(u)} />
            {u}
          </label>
        ))}
      </div>
    </Dialog>
  )
}

export function DiscoverDialog({ onClose }: { onClose: () => void }) {
  const s = useChat()
  return (
    <Dialog visible header="发现群聊" onClose={onClose} footer={false}>
      <div className="discover-list">
        {s.allGroups.length === 0 && <div className="conv-empty">暂无公开群</div>}
        {s.allGroups.map((g) => (
          <div key={g.name} className="discover-item">
            <span className="discover-name">
              {g.name} · {g.member_count} 人
            </span>
            {g.joined ? (
              <span className="joined-tag">已加入</span>
            ) : (
              <Button size="small" theme="primary" onClick={() => joinGroup(g.name)}>
                加入
              </Button>
            )}
          </div>
        ))}
      </div>
    </Dialog>
  )
}

export function MemberDialog({
  name, members, onClose,
}: { name: string; members: string[]; onClose: () => void }) {
  return (
    <Dialog visible header={`${name} 的成员`} onClose={onClose} footer={false}>
      <div className="member-list">
        {members.length === 0 && <div className="conv-empty">暂无成员</div>}
        {members.map((u) => (
          <div key={u} className="member-item">
            <span className="mini-avatar" style={{ background: colorOf(u) }}>
              {initialOf(u)}
            </span>
            {u}
          </div>
        ))}
      </div>
    </Dialog>
  )
}
