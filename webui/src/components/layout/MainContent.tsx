import { Terminal } from '@/components/console/Terminal'
import { useLogWebSocket } from '@/hooks/useWebSocket'

export function MainContent() {
  // Connect to WebSocket for logs
  useLogWebSocket()

  return (
    <main className="relative z-10 h-80 min-h-0 overflow-hidden">
      <Terminal />
    </main>
  )
}
