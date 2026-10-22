import { useState } from 'react'
import { Input, Button, MessagePlugin } from 'tdesign-react'
import { useChat, login } from '../store/chatStore'

export default function Login() {
  const s = useChat()
  const [name, setName] = useState('')

  const submit = () => {
    const v = name.trim()
    if (!v) {
      MessagePlugin.warning('请输入昵称')
      return
    }
    login(v)
  }

  const connecting = s.status === 'connecting'
  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="login-logo">MoyuChat</div>
        <div className="login-sub">登录即注册 · 输入昵称即可开聊</div>
        <Input
          value={name}
          onChange={(v) => setName(v as string)}
          onEnter={submit}
          placeholder="输入昵称"
          size="large"
          disabled={connecting}
        />
        <Button
          theme="primary"
          block
          size="large"
          loading={connecting}
          onClick={submit}
        >
          {s.status === 'kicked' ? '重新进入' : '进入'}
        </Button>
        {s.status === 'kicked' && (
          <div className="login-hint">账号已在别处登录，已被顶下线</div>
        )}
        {s.status === 'disconnected' && s.username && !s.error && (
          <div className="login-hint">连接已断开，正在重连…</div>
        )}
        {s.error && <div className="login-hint">{s.error}</div>}
      </div>
    </div>
  )
}
