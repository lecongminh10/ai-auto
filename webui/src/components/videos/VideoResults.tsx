import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertCircle, CheckCircle2, Download, ExternalLink, Eye, Heart, Languages, Loader2, MessageCircle, RefreshCw, Share2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useCrawlerStore } from '@/store/crawlerStore'

type VideoItem = {
  id: string
  platform: 'dy' | 'xhs' | 'bili'
  title: string
  author: string
  source_url: string
  preview_url: string
  cover_url: string
  views: number
  likes: number
  comments: number
  shares: number
  collections: number
}

const compact = new Intl.NumberFormat('vi-VN', { notation: 'compact', maximumFractionDigits: 1 })

export function VideoResults() {
  const platform = useCrawlerStore((state) => state.config.platform) as VideoItem['platform']
  const status = useCrawlerStore((state) => state.status)
  const [videos, setVideos] = useState<VideoItem[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const [dubbing, setDubbing] = useState(false)
  const [dubVideo, setDubVideo] = useState<VideoItem | null>(null)
  const [transcript, setTranscript] = useState('')
  const [voice, setVoice] = useState('vi-VN-HoaiMyNeural')
  const [dubOutput, setDubOutput] = useState('')
  const [dubbingOpen, setDubbingOpen] = useState(false)
  const [dubStage, setDubStage] = useState('')
  const [dubError, setDubError] = useState('')
  const [dubProgress, setDubProgress] = useState(0)
  const [filters, setFilters] = useState({ minViews: '0', minLikes: '0', minComments: '0', minShares: '0', sortBy: 'views' })

  const loadVideos = useCallback(async () => {
    setLoading(true)
    try {
      const query = new URLSearchParams({
        platform,
        min_views: filters.minViews || '0',
        min_likes: filters.minLikes || '0',
        min_comments: filters.minComments || '0',
        min_shares: filters.minShares || '0',
        sort_by: filters.sortBy,
      })
      const response = await fetch(`/api/videos?${query}`)
      const payload = await response.json()
      setVideos(payload.videos || [])
      setSelected(new Set())
    } finally {
      setLoading(false)
    }
  }, [platform, filters])

  useEffect(() => {
    if (status === 'running') {
      setVideos([])
      setSelected(new Set())
    } else {
      void loadVideos()
    }
  }, [platform, status]) // eslint-disable-line react-hooks/exhaustive-deps

  const selectedVideos = useMemo(() => videos.filter((video) => selected.has(video.id)), [videos, selected])

  const toggle = (id: string) => {
    setSelected((current) => {
      // Lồng tiếng là pipeline đơn video: luôn chỉ giữ tối đa một lựa chọn.
      return current.has(id) ? new Set() : new Set([id])
    })
  }

  const downloadSelected = async () => {
    setDownloading(true)
    try {
      for (const video of selectedVideos) {
        if (!video.preview_url) continue
        const response = await fetch('/api/videos/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ platform: video.platform, video_id: video.id, url: video.preview_url }),
        })
        if (!response.ok) {
          const error = await response.json().catch(() => ({}))
          throw new Error(error.detail || `Không thể tải ${video.id}`)
        }
      }
      alert(`Đã tải ${selectedVideos.filter((video) => video.preview_url).length} video vào thư mục video`)
    } catch (error) {
      alert(error instanceof Error ? error.message : 'Tải video thất bại')
    } finally {
      setDownloading(false)
    }
  }

  const prepareDubbing = async () => {
    const video = selectedVideos[0]
    if (!video || selectedVideos.length !== 1) return
    setDubbingOpen(true)
    setDubbing(true)
    setDubOutput('')
    setDubError('')
    setDubStage('Đang tải video gốc và tách audio...')
    try {
      const response = await fetch('/api/videos/dub/prepare', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ platform: video.platform, video_id: video.id, url: video.preview_url }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(payload.detail || 'Không thể tạo job nhận diện lời thoại')
      for (;;) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000))
        const statusResponse = await fetch(`/api/videos/dub/status/${payload.job_id}?t=${Date.now()}`, { cache: 'no-store' })
        if (!statusResponse.ok) {
          const errorPayload = await statusResponse.json().catch(() => ({}))
          throw new Error(errorPayload.detail || `Không tìm thấy job xử lý (${statusResponse.status})`)
        }
        const job = await statusResponse.json()
        setDubStage(job.stage || 'Đang xử lý...')
        setDubProgress(Number(job.progress || 0))
        if (job.status === 'error') throw new Error(job.error || 'Job nhận diện thất bại')
        if (job.status === 'done') {
          setDubVideo(video)
          setTranscript(job.transcript_srt || '')
          setDubStage('Đã nhận diện xong. Hãy kiểm tra transcript trước khi tiếp tục.')
          break
        }
      }
    } catch (error) {
      setDubError(error instanceof Error ? error.message : 'Chuẩn bị lồng tiếng thất bại')
      setDubStage('Pipeline dừng ở bước nhận diện transcript')
    } finally { setDubbing(false) }
  }

  const renderDubbing = async () => {
    if (!dubVideo || !transcript.trim()) return
    setDubbing(true)
    setDubError('')
    setDubStage('Đang dịch phụ đề Trung → Việt bằng Gemini...')
    try {
      const response = await fetch('/api/videos/dub/render', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ video_id: dubVideo.id, transcript_srt: transcript, voice }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(payload.detail || 'Không thể tạo video lồng tiếng')
      setDubOutput(`/api/videos/dub/output/${encodeURIComponent(dubVideo.id)}`)
      setDubStage('Hoàn tất: video lồng tiếng đã sẵn sàng.')
    } catch (error) {
      setDubError(error instanceof Error ? error.message : 'Lồng tiếng thất bại')
      setDubStage('Pipeline dừng khi dịch/TTS/ghép video')
    } finally { setDubbing(false) }
  }

  return (
    <section className="flex-shrink-0 rounded-lg glass-panel border border-cyber-border-subtle overflow-hidden">
      <header className="px-4 py-2.5 flex flex-wrap items-center gap-2 border-b border-cyber-border-subtle bg-cyber-bg-tertiary/30">
        <strong className="mr-auto text-xs font-mono text-cyber-text-primary">KẾT QUẢ VIDEO ({videos.length})</strong>
        {(['minViews', 'minLikes', 'minComments', 'minShares'] as const).map((key) => (
          <Input
            key={key}
            type="number"
            min="0"
            value={filters[key]}
            onChange={(event) => setFilters({ ...filters, [key]: event.target.value })}
            placeholder={{ minViews: 'View tối thiểu', minLikes: 'Like tối thiểu', minComments: 'Bình luận', minShares: 'Chia sẻ' }[key]}
            title={{ minViews: 'Lượt xem tối thiểu', minLikes: 'Lượt thích tối thiểu', minComments: 'Bình luận tối thiểu', minShares: 'Chia sẻ tối thiểu' }[key]}
            disabled={key === 'minViews' && platform !== 'bili'}
            className="h-8 w-28 text-[11px]"
          />
        ))}
        <Select value={filters.sortBy} onValueChange={(sortBy) => setFilters({ ...filters, sortBy })}>
          <SelectTrigger className="h-8 w-32 text-[11px]"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="views">Nhiều view nhất</SelectItem>
            <SelectItem value="likes">Nhiều like nhất</SelectItem>
            <SelectItem value="comments">Nhiều bình luận</SelectItem>
            <SelectItem value="shares">Nhiều chia sẻ</SelectItem>
            <SelectItem value="collections">Nhiều lượt lưu</SelectItem>
          </SelectContent>
        </Select>
        <Button size="sm" variant="ghost" className="h-8" onClick={loadVideos} disabled={loading}>
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} /> Lọc
        </Button>
        <Button size="sm" className="h-8" onClick={downloadSelected} disabled={!selectedVideos.some((video) => video.preview_url) || downloading}>
          <Download className="h-4 w-4" /> Tải video đã chọn
        </Button>
        <Button size="sm" variant="outline" className="h-8" onClick={prepareDubbing}
          disabled={platform !== 'dy' || selectedVideos.length !== 1 || !selectedVideos[0]?.preview_url || dubbing}
          title={platform !== 'dy' ? 'Hiện chỉ hỗ trợ Douyin' : 'Chọn đúng một video'}>
          <Languages className="h-4 w-4" /> {dubbing ? 'Đang xử lý...' : 'Lồng tiếng Việt'}
        </Button>
      </header>

      <div className="max-h-[300px] overflow-auto p-3">
        {videos.length === 0 ? (
          <p className="py-8 text-center text-xs text-cyber-text-muted">Hãy crawl theo từ khóa, sau đó kết quả sẽ xuất hiện tại đây để lọc và xem trước.</p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
            {videos.map((video) => (
              <article key={video.id} className={`rounded-md border overflow-hidden bg-cyber-bg-tertiary/40 ${selected.has(video.id) ? 'border-cyber-neon-cyan' : 'border-cyber-border-subtle'}`}>
                <div className="relative aspect-video bg-black">
                  {video.preview_url ? (
                    <video
                      controls
                      preload="none"
                      poster={video.cover_url}
                      src={`/api/videos/stream?platform=${video.platform}&video_id=${encodeURIComponent(video.id)}`}
                      className="h-full w-full object-contain"
                    />
                  ) : video.cover_url ? (
                    <img src={video.cover_url} alt="" className="h-full w-full object-cover" />
                  ) : (
                    <div className="h-full flex items-center justify-center text-xs text-cyber-text-muted">Không có preview</div>
                  )}
                  <input type="checkbox" checked={selected.has(video.id)} onChange={() => toggle(video.id)} className="absolute left-2 top-2 h-4 w-4 accent-cyan-400" />
                </div>
                <div className="p-2 space-y-1.5">
                  <p className="text-xs line-clamp-2 text-cyber-text-primary" title={video.title}>{video.title}</p>
                  <div className="flex gap-3 text-[10px] text-cyber-text-muted">
                    <span title={video.views ? 'Lượt xem' : 'Nền tảng không cung cấp lượt xem'}><Eye className="inline h-3 w-3" /> {video.views ? compact.format(video.views) : '—'}</span>
                    <span><Heart className="inline h-3 w-3" /> {compact.format(video.likes)}</span>
                    <span><MessageCircle className="inline h-3 w-3" /> {compact.format(video.comments)}</span>
                    <span><Share2 className="inline h-3 w-3" /> {compact.format(video.shares)}</span>
                    {video.source_url && <a href={video.source_url} target="_blank" rel="noreferrer" className="ml-auto text-cyber-neon-cyan"><ExternalLink className="h-3 w-3" /></a>}
                  </div>
                </div>
              </article>
            ))}
          </div>
        )}
      </div>
      {dubbingOpen && (
        <div className="fixed inset-x-4 bottom-4 top-20 z-[80] overflow-y-auto rounded-lg border border-cyber-neon-cyan/60 p-4 shadow-2xl space-y-3 bg-cyber-bg-tertiary">
          <div className="flex items-center gap-2">
            <Languages className="h-4 w-4 text-cyber-neon-cyan" />
            <strong className="text-xs text-cyber-text-primary">GIAO DIỆN LỒNG TIẾNG TRUNG → VIỆT {dubVideo ? `— ${dubVideo.id}` : ''}</strong>
            <button className="ml-auto text-xs text-cyber-text-muted" onClick={() => setDubbingOpen(false)}>Đóng</button>
          </div>
          <div className="flex items-center gap-2 rounded border border-cyber-border-subtle bg-black/30 p-3 text-xs">
            {dubError ? <AlertCircle className="h-4 w-4 text-red-400" /> : dubOutput ? <CheckCircle2 className="h-4 w-4 text-green-400" /> : <Loader2 className="h-4 w-4 animate-spin text-cyber-neon-cyan" />}
            <span className={dubError ? 'text-red-300' : 'text-cyber-text-primary'}>{dubStage || 'Đang khởi tạo pipeline...'}</span>
          </div>
          {dubbing && <div className="h-1.5 overflow-hidden rounded bg-black/50"><div className="h-full rounded bg-cyber-neon-cyan transition-all duration-500" style={{ width: `${Math.max(4, dubProgress)}%` }} /></div>}
          {dubError && <pre className="max-h-32 overflow-auto rounded border border-red-400/40 bg-red-950/20 p-3 whitespace-pre-wrap text-[11px] text-red-300">{dubError}</pre>}
          {dubVideo && <>
            <p className="text-[11px] text-cyber-text-muted">Sửa nội dung nếu Whisper nhận sai. Giữ nguyên số thứ tự và timestamp SRT.</p>
            <textarea value={transcript} onChange={(event) => setTranscript(event.target.value)} disabled={dubbing}
              className="h-52 w-full rounded border border-cyber-border-subtle bg-black/50 p-3 font-mono text-xs text-cyber-text-primary" />
            <div className="flex flex-wrap items-center gap-2">
              <Select value={voice} onValueChange={setVoice}>
                <SelectTrigger className="h-8 w-52 text-xs"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="vi-VN-HoaiMyNeural">Hoài My (nữ)</SelectItem>
                  <SelectItem value="vi-VN-NamMinhNeural">Nam Minh (nam)</SelectItem>
                </SelectContent>
              </Select>
              <Button size="sm" onClick={renderDubbing} disabled={dubbing || !transcript.trim()}>
                <Languages className="h-4 w-4" /> {dubbing ? 'Đang dịch và ghép...' : 'Transcript đúng — tiến hành lồng tiếng'}
              </Button>
              {dubOutput && <a href={dubOutput} className="text-xs text-cyber-neon-cyan underline">Tải output_vi.mp4</a>}
            </div>
          </>}
        </div>
      )}
    </section>
  )
}
