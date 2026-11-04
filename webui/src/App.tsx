import { useState } from 'react'
import { Toaster } from 'sonner'
import { Sidebar } from '@/components/layout/Sidebar'
import { MainContent } from '@/components/layout/MainContent'
import { CrawlerConfigPanel } from '@/components/config/CrawlerConfigPanel'
import { VideoResults } from '@/components/videos/VideoResults'
import { EnvironmentCheck, isEnvChecked } from '@/components/env/EnvironmentCheck'
import { DouyinLoginQr } from '@/components/auth/DouyinLoginQr'

function App() {
  // Initialize by checking localStorage if env check has passed
  const [envChecked, setEnvChecked] = useState(() => isEnvChecked())

  const handleEnvCheckComplete = () => {
    setEnvChecked(true)
  }

  return (
    <div className="flex min-h-screen flex-col cyber-grid relative">
      {/* Kiểm tra môi trường khi mở lần đầu */}
      {!envChecked && (
        <EnvironmentCheck onCheckComplete={handleEnvCheckComplete} />
      )}
      <DouyinLoginQr />

      {/* Header Bar */}
      <Sidebar />

      {/* Main Area */}
      <div className="flex flex-col gap-4 p-4">
        {/* Config Panel - Primary Action Area (Always Expanded) */}
        <div className="flex-shrink-0">
          <CrawlerConfigPanel />
        </div>

        <VideoResults />

        {/* Console - Collapsible Terminal */}
        <MainContent />
      </div>

      {/* Toast notifications - Theme-aware style */}
      <Toaster
        position="top-right"
        toastOptions={{
          className: 'glass-panel font-mono text-cyber-text-primary',
          style: {
            fontFamily: 'JetBrains Mono, monospace',
          },
        }}
      />
    </div>
  )
}

export default App
