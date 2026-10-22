import { useChat } from '../store/chatStore'
import ConversationList from './ConversationList'
import ChatPanel from './ChatPanel'

export default function MainLayout() {
  const s = useChat()
  return (
    <div className="app">
      <ConversationList />
      {s.active ? (
        <ChatPanel />
      ) : (
        <div className="empty-pane">
          <div className="empty-inner">选择一个会话开始聊天</div>
        </div>
      )}
    </div>
  )
}
