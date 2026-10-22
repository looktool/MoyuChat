import { useChat } from './store/chatStore'
import Login from './components/Login'
import MainLayout from './components/MainLayout'

export default function App() {
  const s = useChat()
  if (s.status === 'online' && s.username) return <MainLayout />
  return <Login />
}
