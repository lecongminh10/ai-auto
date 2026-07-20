import { useEffect, useState } from 'react'
import { Loader2, QrCode } from 'lucide-react'
import { useCrawlerStore } from '@/store/crawlerStore'
import { workspaceFetch } from '@/lib/workspace'

export function DouyinLoginQr() {
  const status = useCrawlerStore((state) => state.status)
  const platform = useCrawlerStore((state) => state.platform)
  const [qrUrl, setQrUrl] = useState('')

  useEffect(() => {
    if (status !== 'running' || platform !== 'dy') {
      setQrUrl((current) => {
        if (current) URL.revokeObjectURL(current)
        return ''
      })
      return
    }

    let cancelled = false
    const loadQr = async () => {
      try {
        const response = await workspaceFetch(`/api/crawler/login-qrcode?t=${Date.now()}`, { cache: 'no-store' })
        if (response.status === 204 || !response.ok) {
          setQrUrl((current) => {
            if (current) URL.revokeObjectURL(current)
            return ''
          })
          return
        }
        const next = URL.createObjectURL(await response.blob())
        if (cancelled) return URL.revokeObjectURL(next)
        setQrUrl((current) => {
          if (current) URL.revokeObjectURL(current)
          return next
        })
      } catch {
        // QR chưa được crawler tạo; lần poll tiếp theo sẽ thử lại.
      }
    }
    void loadQr()
    const timer = window.setInterval(loadQr, 1000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [platform, status])

  if (!qrUrl) return null
  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/75 p-4 backdrop-blur-sm">
      <div className="w-full max-w-sm rounded-lg border border-cyber-neon-cyan bg-[#0d1117] p-6 text-center shadow-2xl">
        <QrCode className="mx-auto mb-2 h-7 w-7 text-cyber-neon-cyan" />
        <h2 className="text-base font-semibold text-cyber-text-primary">Đăng nhập Douyin lần đầu</h2>
        <p className="mt-2 text-xs text-cyber-text-muted">Mở ứng dụng Douyin trên điện thoại và quét mã này. Phiên đăng nhập sẽ được lưu cho những lần chạy sau.</p>
        <div className="mx-auto my-4 w-fit rounded-lg bg-white p-3">
          <img src={qrUrl} alt="QR đăng nhập Douyin" className="h-56 w-56 object-contain" />
        </div>
        <div className="flex items-center justify-center gap-2 text-xs text-cyber-neon-green">
          <Loader2 className="h-4 w-4 animate-spin" /> Đang chờ bạn xác nhận đăng nhập...
        </div>
      </div>
    </div>
  )
}
