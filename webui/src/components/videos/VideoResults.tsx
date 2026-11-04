import { useCallback, useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { AlertCircle, CheckCircle2, Clock3, Download, ExternalLink, Eye, Heart, Languages, Loader2, MessageCircle, RefreshCw, Send, Share2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useCrawlerStore } from '@/store/crawlerStore'
import { withWorkspace, workspaceFetch } from '@/lib/workspace'
import { useStartCrawler } from '@/hooks/useCrawler'

type VideoItem = {
  id: string
  platform: 'dy' | 'xhs' | 'bili'
  creator_hash: string
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
  duration_seconds: number
  published_at: number
  dubbed_available?: boolean
}

const compact = new Intl.NumberFormat('vi-VN', { notation: 'compact', maximumFractionDigits: 1 })

function readableError(value: unknown, fallback: string): string {
  if (typeof value === 'string' && value.trim()) return value
  if (Array.isArray(value)) {
    const messages = value
      .map((item) => readableError(item, ''))
      .filter(Boolean)
    return messages.join('; ') || fallback
  }
  if (value && typeof value === 'object') {
    const detail = value as { detail?: unknown; msg?: unknown; message?: unknown; loc?: unknown }
    if (detail.detail !== undefined) return readableError(detail.detail, fallback)
    const message = readableError(detail.msg ?? detail.message, '')
    const location = Array.isArray(detail.loc)
      ? detail.loc.filter((part) => part !== 'body').join('.')
      : ''
    if (message) return location ? `${location}: ${message}` : message
    try {
      return JSON.stringify(value)
    } catch {
      return fallback
    }
  }
  return fallback
}

function friendlyDubbingError(message: string): string {
  if (/Number of bands|Reserved bit set|Prediction is not allowed in AAC|Invalid data found when processing input/i.test(message)) {
    return 'Âm thanh AAC trong video bị lỗi nên FFmpeg không giải mã được. Hệ thống sẽ thử bỏ qua packet hỏng; nếu vẫn lỗi, hãy tải lại hoặc bỏ video này khỏi lô.'
  }
  if (/Lệnh ffmpeg thất bại/i.test(message)) {
    return 'FFmpeg xử lý video thất bại. Hãy tải lại video hoặc bỏ video này khỏi lô; xem log server để biết chi tiết kỹ thuật.'
  }
  return message
}

async function runWithConcurrency<T>(items: T[], concurrency: number, worker: (item: T, index: number) => Promise<void>) {
  let nextIndex = 0
  const workerCount = Math.min(items.length, Math.max(1, concurrency))
  await Promise.all(Array.from({ length: workerCount }, async () => {
    while (nextIndex < items.length) {
      const index = nextIndex++
      await worker(items[index], index)
    }
  }))
}

export function VideoResults() {
  const platform = useCrawlerStore((state) => state.config.platform) as VideoItem['platform']
  const configuredMinDuration = useCrawlerStore((state) => state.config.min_video_duration_seconds)
  const status = useCrawlerStore((state) => state.status)
  const crawlerLogs = useCrawlerStore((state) => state.logs)
  const crawlerConfig = useCrawlerStore((state) => state.config)
  const updateConfig = useCrawlerStore((state) => state.updateConfig)
  const { mutate: startCrawler, isPending: isStartingCreator } = useStartCrawler()
  const [creatorVideoId, setCreatorVideoId] = useState<string | null>(null)
  const [channelVideo, setChannelVideo] = useState<VideoItem | null>(null)
  const [channelVideos, setChannelVideos] = useState<VideoItem[]>([])
  const [channelLoading, setChannelLoading] = useState(false)
  const [channelCrawlStarted, setChannelCrawlStarted] = useState(false)
  const [creatorCrawlHash, setCreatorCrawlHash] = useState<string | null>(null)
  const [channelError, setChannelError] = useState('')
  const [channelSelected, setChannelSelected] = useState<Set<string>>(new Set())
  const [batchRunning, setBatchRunning] = useState(false)
  const [videoParallelism, setVideoParallelism] = useState(() => {
    try {
      const value = Number(window.localStorage.getItem('mediacrawler:video-parallelism') || 2)
      return Math.min(4, Math.max(1, Number.isFinite(value) ? value : 2))
    } catch { return 2 }
  })
  const [batchStatus, setBatchStatus] = useState<Record<string, { status: string; message: string; detail?: string; progress?: number; checkedAt?: number; stageSince?: number }>>({})
  const [autoDownloadOnComplete, setAutoDownloadOnComplete] = useState(() => {
    try { return window.localStorage.getItem('mediacrawler:dub-auto-default') === 'true' } catch { return false }
  })
  const [videos, setVideos] = useState<VideoItem[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const [downloadProgress, setDownloadProgress] = useState({ completed: 0, total: 0, failed: 0 })
  const [socialSending, setSocialSending] = useState(false)
  const [dubbing, setDubbing] = useState(false)
  const [dubVideo, setDubVideo] = useState<VideoItem | null>(null)
  const [transcript, setTranscript] = useState('')
  const [voice, setVoice] = useState('vi-VN-HoaiMyNeural')
  const [mood, setMood] = useState('natural')
  const [translationStyle, setTranslationStyle] = useState('faithful')
  const [dubOutput, setDubOutput] = useState('')
  const [dubbingOpen, setDubbingOpen] = useState(false)
  const [dubStage, setDubStage] = useState('')
  const [dubError, setDubError] = useState('')
  const [dubProgress, setDubProgress] = useState(0)
  const [dubLogs, setDubLogs] = useState<string[]>([])
  const [filters, setFilters] = useState({ minViews: '0', minLikes: '0', minComments: '0', minShares: '0', minDuration: String(configuredMinDuration), sortBy: 'newest' })

  const loadVideos = useCallback(async () => {
    setLoading(true)
    try {
      const query = new URLSearchParams({
        platform,
        min_views: filters.minViews || '0',
        min_likes: filters.minLikes || '0',
        min_comments: filters.minComments || '0',
        min_shares: filters.minShares || '0',
        min_duration_seconds: filters.minDuration || '0',
        sort_by: filters.sortBy,
      })
      const response = await workspaceFetch(`/api/videos?${query}`)
      const payload = await response.json()
      setVideos(payload.videos || [])
      setSelected(new Set())
    } finally {
      setLoading(false)
    }
  }, [platform, filters])

  useEffect(() => {
    if (status === 'running') {
      // Keep the current library visible while a new crawl runs. The backend
      // stages old result files during the run, but clearing React state here
      // made the whole results area appear empty until the crawl finished.
    } else if (!creatorCrawlHash) {
      void loadVideos()
    }
  }, [platform, status, creatorCrawlHash, loadVideos])

  useEffect(() => {
    setFilters((current) => ({ ...current, minDuration: String(configuredMinDuration) }))
  }, [configuredMinDuration])

  const selectedVideos = useMemo(() => videos.filter((video) => selected.has(video.id)), [videos, selected])
  const captchaRequired = crawlerLogs.some((entry) => /captcha|验证码中间页/i.test(entry.message))

  const toggle = (id: string) => {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const loadCreatorVideos = useCallback(async (creatorHash: string, showError = true, includeUnmatched = false): Promise<VideoItem[]> => {
    if (showError) setChannelLoading(true)
    try {
      const query = new URLSearchParams({ platform: 'dy', creator_hash: creatorHash, sort_by: 'newest' })
      const response = await workspaceFetch(`/api/videos?${query}`, { cache: 'no-store' })
      const payload = await response.json()
      if (!response.ok) throw new Error(readableError(payload, 'Không tải được video của kênh'))
      let creatorVideos = (payload.videos || []) as VideoItem[]
      // A few Douyin responses omit creator_hash on individual items. During
      // this isolated crawl, those items are still valid results for the
      // selected creator; the crawler manager has moved older result files
      // aside, so an unfiltered read only contains this run's output.
      if (includeUnmatched && creatorVideos.length === 0) {
        const allQuery = new URLSearchParams({ platform: 'dy', sort_by: 'newest' })
        const allResponse = await workspaceFetch(`/api/videos?${allQuery}`, { cache: 'no-store' })
        const allPayload = await allResponse.json()
        if (!allResponse.ok) throw new Error(readableError(allPayload, 'Không tải được video của kênh'))
        creatorVideos = (allPayload.videos || []) as VideoItem[]
      }
      // Starting a creator crawl temporarily moves the previous JSONL files
      // out of the active data directory. Keep the already loaded snapshot
      // until this run has written its first records, instead of replacing the
      // popup with an empty list on every poll.
      if (!includeUnmatched || creatorVideos.length > 0) setChannelVideos(creatorVideos)
      return creatorVideos
    } catch (error) {
      if (showError) setChannelError(error instanceof Error ? error.message : 'Không tải được video của kênh')
      return []
    } finally {
      if (showError) setChannelLoading(false)
    }
  }, [])

  useEffect(() => {
    if (status === 'running') {
      if (creatorCrawlHash && !channelCrawlStarted) setChannelCrawlStarted(true)
      if (!creatorCrawlHash) return
      let stopped = false
      const refreshCreatorResults = async () => {
        while (!stopped) {
          const result = await loadCreatorVideos(creatorCrawlHash, false, true)
          if (!stopped && result.length > 0) setVideos(result)
          await new Promise((resolve) => window.setTimeout(resolve, 2500))
        }
      }
      void refreshCreatorResults()
      return () => { stopped = true }
    }
    if (creatorCrawlHash && channelCrawlStarted && (status === 'idle' || status === 'error')) {
      let stopped = false
      void loadCreatorVideos(creatorCrawlHash, false, true).then((result) => {
        if (stopped) return
        setVideos(result)
        setSelected(new Set())
        setChannelCrawlStarted(false)
        setCreatorCrawlHash(null)
      })
      return () => { stopped = true }
    }
  }, [status, creatorCrawlHash, channelCrawlStarted, loadCreatorVideos])

  const crawlVideoCreator = async (video: VideoItem) => {
    if (video.platform !== 'dy' || status !== 'idle') return
    if (!video.creator_hash) {
      setChannelVideo(video)
      setChannelError('Video này chưa có mã nhóm nhà sáng tạo. Hãy crawl lại video bằng bản mới rồi thử lại.')
      return
    }
    setChannelVideo(video)
    setChannelVideos([])
    setChannelSelected(new Set())
    setChannelError('')
    setChannelLoading(true)
    setChannelCrawlStarted(false)
    setCreatorVideoId(video.id)
    setCreatorCrawlHash(video.creator_hash)
    updateConfig({ platform: 'dy' })
    // Show videos already in the local library before starting the full crawl.
    // A creator crawl can take a while, and the manager temporarily archives
    // the result files while it runs, so load this snapshot first.
    await loadCreatorVideos(video.creator_hash)
    startCrawler(
      { ...crawlerConfig, platform: 'dy', crawler_type: 'creator', creator_ids: `video:${video.id}` },
      {
        onError: (error) => {
          setChannelError(error.message || 'Không thể khởi chạy crawl kênh')
          setChannelLoading(false)
          setCreatorVideoId(null)
          setCreatorCrawlHash(null)
          setChannelCrawlStarted(false)
        },
        onSuccess: () => setCreatorVideoId(null),
      },
    )
  }

  const toggleChannelVideo = (id: string) => {
    setChannelSelected((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const closeCreatorPopup = () => {
    if (channelVideos.length > 0) {
      setVideos(channelVideos)
      setSelected(new Set())
    }
    setChannelVideo(null)
  }

  const downloadDubbedVideo = useCallback((videoId: string) => {
    const link = document.createElement('a')
    link.href = withWorkspace(`/api/videos/dub/output/${encodeURIComponent(videoId)}`)
    link.download = ''
    link.style.display = 'none'
    document.body.appendChild(link)
    link.click()
    link.remove()
  }, [])

  const toggleAutoDownload = () => {
    setAutoDownloadOnComplete((enabled) => {
      const next = !enabled
      window.localStorage.setItem('mediacrawler:dub-auto-default', String(next))
      return next
    })
  }

  const runBatchDubbing = async (items: VideoItem[] = channelVideos, selectedIds: Set<string> = channelSelected) => {
    const queue = items.filter((video) => selectedIds.has(video.id) && video.preview_url)
    if (!queue.length || batchRunning) return
    setBatchRunning(true)
    setBatchStatus((current) => ({
      ...current,
      ...Object.fromEntries(queue.map((video) => [video.id, { status: 'queued', message: 'Đang chờ worker...', progress: 0 }])),
    }))
    try {
      await runWithConcurrency(queue, videoParallelism, async (video) => {
        let stageSince = Date.now()
        const updateProgress = (status: 'preparing' | 'rendering', job: Record<string, unknown>) => {
          const message = String(job.stage || (status === 'preparing' ? 'Đang nhận diện lời thoại' : 'Đang lồng tiếng'))
          setBatchStatus((current) => {
            const previous = current[video.id]
            if (previous?.message !== message) stageSince = Date.now()
            return {
              ...current,
              [video.id]: {
                status,
                message,
                progress: Number(job.progress || 0),
                detail: Array.isArray(job.logs) ? String(job.logs[job.logs.length - 1] || '') : '',
                checkedAt: Date.now(),
                stageSince,
              },
            }
          })
        }
        const waitForJob = async (jobId: string, status: 'preparing' | 'rendering') => {
          while (true) {
            await new Promise((resolve) => window.setTimeout(resolve, 1000))
            const response = await workspaceFetch(`/api/videos/dub/status/${encodeURIComponent(jobId)}?t=${Date.now()}`, { cache: 'no-store' })
            const job = await response.json().catch(() => ({}))
            if (!response.ok) throw new Error(readableError(job, `Không tìm thấy job xử lý (${response.status})`))
            updateProgress(status, job)
            if (job.status === 'error') throw new Error(readableError(job.error, 'Lồng tiếng thất bại'))
            if (job.status === 'done') return job
          }
        }

        try {
          stageSince = Date.now()
          setBatchStatus((current) => ({ ...current, [video.id]: { status: 'preparing', message: 'Đang tải video và nhận diện lời thoại', progress: 0, checkedAt: Date.now(), stageSince } }))
          const prepareResponse = await workspaceFetch('/api/videos/dub/prepare', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ platform: 'dy', video_id: video.id, url: video.preview_url }),
          })
          const preparePayload = await prepareResponse.json().catch(() => ({}))
          if (!prepareResponse.ok) throw new Error(readableError(preparePayload, 'Không thể chuẩn bị video'))
          const prepared = await waitForJob(preparePayload.job_id, 'preparing')

          stageSince = Date.now()
          setBatchStatus((current) => ({ ...current, [video.id]: { status: 'rendering', message: 'Đang dịch và tạo giọng đọc', progress: 0, checkedAt: Date.now(), stageSince } }))
          const renderResponse = await workspaceFetch('/api/videos/dub/render', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              video_id: video.id,
              transcript_srt: prepared.transcript_srt || '',
              voice,
              mood,
              translation_style: translationStyle,
            }),
          })
          const renderPayload = await renderResponse.json().catch(() => ({}))
          if (!renderResponse.ok) throw new Error(readableError(renderPayload, 'Không thể bắt đầu dịch và lồng tiếng'))
          await waitForJob(renderPayload.job_id, 'rendering')
          setBatchStatus((current) => ({ ...current, [video.id]: { status: 'done', message: 'Hoàn tất', progress: 100 } }))
          setChannelVideos((current) => current.map((item) => item.id === video.id ? { ...item, dubbed_available: true } : item))
          if (autoDownloadOnComplete) downloadDubbedVideo(video.id)
        } catch (error) {
          setBatchStatus((current) => ({
            ...current,
            [video.id]: { status: 'error', message: friendlyDubbingError(readableError(error, 'Lồng tiếng thất bại')) },
          }))
        }
      })
    } finally {
      setBatchRunning(false)
    }
  }

  const downloadSelected = async () => {
    const queue = selectedVideos.filter((video) => video.preview_url)
    if (!queue.length) return
    setDownloading(true)
    setDownloadProgress({ completed: 0, total: queue.length, failed: 0 })
    const failures: string[] = []
    try {
      await runWithConcurrency(queue, videoParallelism, async (video) => {
        let failed = false
        try {
          const response = await workspaceFetch('/api/videos/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ platform: video.platform, video_id: video.id, url: video.preview_url }),
          })
          if (!response.ok) {
            const error = await response.json().catch(() => ({}))
            throw new Error(readableError(error, `Không thể tải ${video.id}`))
          }
        } catch (error) {
          failed = true
          failures.push(`${video.id}: ${error instanceof Error ? error.message : 'Tải thất bại'}`)
        } finally {
          setDownloadProgress((current) => ({
            ...current,
            completed: current.completed + 1,
            failed: current.failed + (failed ? 1 : 0),
          }))
        }
      })
      await loadVideos()
      const succeeded = queue.length - failures.length
      alert(failures.length
        ? `Đã tải ${succeeded}/${queue.length} video. ${failures.length} video lỗi:\n${failures.slice(0, 5).join('\n')}`
        : `Đã tải xong ${succeeded} video vào thư mục video.`)
    } finally {
      setDownloading(false)
    }
  }

  const updateVideoParallelism = (value: string) => {
    const next = Math.min(4, Math.max(1, Number(value) || 1))
    setVideoParallelism(next)
    try { window.localStorage.setItem('mediacrawler:video-parallelism', String(next)) } catch { /* storage optional */ }
  }

  const sendSelectedToMediaSocial = async () => {
    const video = selectedVideos[0]
    if (!video || selectedVideos.length !== 1) return
    setSocialSending(true)
    try {
      const response = await workspaceFetch('/api/videos/social/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ platform: video.platform, video_id: video.id }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(readableError(payload, 'Không thể tạo job gửi MediaSocial'))
      for (;;) {
        await new Promise((resolve) => window.setTimeout(resolve, 750))
        const statusResponse = await workspaceFetch(`/api/videos/social/status/${payload.job_id}?t=${Date.now()}`, { cache: 'no-store' })
        const job = await statusResponse.json().catch(() => ({}))
        if (!statusResponse.ok) throw new Error(readableError(job, 'Không tìm thấy job gửi MediaSocial'))
        if (job.status === 'error') throw new Error(readableError(job.error, 'Gửi MediaSocial thất bại'))
        if (job.status === 'done') {
          alert(`Đã gửi bản lồng tiếng của video ${video.id} sang MediaSocial`)
          break
        }
      }
    } catch (error) {
      alert(error instanceof Error ? error.message : 'Gửi MediaSocial thất bại')
    } finally {
      setSocialSending(false)
    }
  }

  const prepareDubbing = async (channelTarget?: VideoItem) => {
    const video = channelTarget || (selectedVideos.length === 1 ? selectedVideos[0] : null)
    if (!video) return
    if (video.platform !== 'dy' || !video.preview_url) {
      alert('Video này chưa có đường dẫn video để lồng tiếng.')
      return
    }
    setChannelVideo(null)
    setDubbingOpen(true)
    setDubbing(true)
    setDubOutput('')
    setDubError('')
    setDubLogs([])
    setDubStage('Đang tải video gốc và tách audio...')
    try {
      const response = await workspaceFetch('/api/videos/dub/prepare', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ platform: video.platform, video_id: video.id, url: video.preview_url }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(readableError(payload, 'Không thể tạo job nhận diện lời thoại'))
      for (;;) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000))
        const statusResponse = await workspaceFetch(`/api/videos/dub/status/${payload.job_id}?t=${Date.now()}`, { cache: 'no-store' })
        if (!statusResponse.ok) {
          const errorPayload = await statusResponse.json().catch(() => ({}))
          throw new Error(readableError(errorPayload, `Không tìm thấy job xử lý (${statusResponse.status})`))
        }
        const job = await statusResponse.json()
        setDubStage(job.stage || 'Đang xử lý...')
        setDubProgress(Number(job.progress || 0))
        setDubLogs(Array.isArray(job.logs) ? job.logs : [])
        if (job.status === 'error') throw new Error(readableError(job.error, 'Job nhận diện thất bại'))
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
    const shouldAutoDownload = autoDownloadOnComplete
    setDubbing(true)
    setDubError('')
    setDubLogs([])
    setDubProgress(0)
    setDubStage('Đang dịch phụ đề Trung → Việt bằng Gemini...')
    try {
      const response = await workspaceFetch('/api/videos/dub/render', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          video_id: dubVideo.id,
          transcript_srt: transcript,
          voice,
          mood,
          translation_style: translationStyle,
        }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(readableError(payload, 'Không thể tạo video lồng tiếng'))
      for (;;) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000))
        const statusResponse = await workspaceFetch(`/api/videos/dub/status/${payload.job_id}?t=${Date.now()}`, { cache: 'no-store' })
        const job = await statusResponse.json().catch(() => ({}))
        if (!statusResponse.ok) throw new Error(readableError(job, `Không tìm thấy job lồng tiếng (${statusResponse.status})`))
        setDubStage(job.stage || 'Đang lồng tiếng...')
        setDubProgress(Number(job.progress || 0))
        setDubLogs(Array.isArray(job.logs) ? job.logs : [])
        if (job.status === 'error') throw new Error(readableError(job.error, 'Job lồng tiếng thất bại'))
        if (job.status === 'done') {
          setDubOutput(job.output_url || `/api/videos/dub/output/${encodeURIComponent(dubVideo.id)}`)
          setDubStage('Hoàn tất: video lồng tiếng đã sẵn sàng.')
          if (shouldAutoDownload) downloadDubbedVideo(dubVideo.id)
          break
        }
      }
    } catch (error) {
      setDubError(error instanceof Error ? error.message : 'Lồng tiếng thất bại')
      setDubStage('Pipeline dừng khi dịch/TTS/ghép video')
    } finally { setDubbing(false) }
  }

  const batchEntries = Object.values(batchStatus)
  const batchDoneCount = batchEntries.filter((item) => item.status === 'done').length
  const batchErrorCount = batchEntries.filter((item) => item.status === 'error').length

  return (
    <section className="rounded-lg glass-panel border border-cyber-border-subtle overflow-hidden">
      <header className="px-4 py-2.5 flex flex-wrap items-center gap-2 border-b border-cyber-border-subtle bg-cyber-bg-tertiary/30">
        <strong className="mr-auto text-xs font-mono text-cyber-text-primary">KẾT QUẢ VIDEO ({videos.length})</strong>
        {batchEntries.length > 0 && (
          <span className={`text-[11px] ${batchRunning ? 'text-cyan-200' : batchErrorCount ? 'text-red-300' : 'text-emerald-300'}`}>
            {batchRunning ? 'Đang xử lý lô' : batchErrorCount ? 'Lô kết thúc, có video lỗi' : 'Lô đã hoàn tất'} · {batchDoneCount}/{batchEntries.length} xong · {batchErrorCount} lỗi
          </span>
        )}
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
        <Input
          type="number"
          min="0"
          step="1"
          value={Number(filters.minDuration || 0) / 60}
          onChange={(event) => setFilters({ ...filters, minDuration: String(Math.max(0, Number(event.target.value || 0) * 60)) })}
          placeholder="Phút tối thiểu"
          title="Chỉ hiện video dài hơn số phút này"
          disabled={!['dy', 'bili'].includes(platform)}
          className="h-8 w-28 text-[11px]"
        />
        <Select value={filters.sortBy} onValueChange={(sortBy) => setFilters({ ...filters, sortBy })}>
          <SelectTrigger className="h-8 w-32 text-[11px]"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="newest">Mới đăng gần đây</SelectItem>
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
        <Select value={String(videoParallelism)} onValueChange={updateVideoParallelism}>
          <SelectTrigger className="h-8 w-28 text-[11px]" title="Số video tải/lồng tiếng cùng lúc"><SelectValue /></SelectTrigger>
          <SelectContent>
            {[1, 2, 3, 4].map((count) => <SelectItem key={count} value={String(count)}>{count} video cùng lúc</SelectItem>)}
          </SelectContent>
        </Select>
        <Button size="sm" className="h-8" onClick={downloadSelected} disabled={!selectedVideos.some((video) => video.preview_url) || downloading}>
          <Download className="h-4 w-4" /> {downloading ? `Đang tải ${downloadProgress.completed}/${downloadProgress.total} · lỗi ${downloadProgress.failed}` : 'Tải video đã chọn'}
        </Button>
        <Button size="sm" variant="outline" className="h-8" onClick={() => void prepareDubbing()}
          disabled={platform !== 'dy' || selectedVideos.length !== 1 || !selectedVideos[0]?.preview_url || dubbing || batchRunning}
          title={platform !== 'dy' ? 'Hiện chỉ hỗ trợ Douyin' : 'Chọn một video để lồng tiếng riêng'}>
          <Languages className="h-4 w-4" /> {dubbing ? 'Đang xử lý...' : 'Lồng tiếng 1 video'}
        </Button>
        {platform === 'dy' && selectedVideos.length > 1 && (
          <Button size="sm" variant="outline" className="h-8" onClick={() => void runBatchDubbing(videos, selected)} disabled={batchRunning || dubbing}>
            <Languages className="h-4 w-4" /> {batchRunning ? 'Đang chạy lô...' : `Dịch & lồng tiếng ${selectedVideos.length} video`}
          </Button>
        )}
        <Button size="sm" variant="outline" className="h-8" onClick={sendSelectedToMediaSocial}
          disabled={selectedVideos.length !== 1 || socialSending}
          title="Chỉ gửi output_vi.mp4 sau khi đã lồng tiếng">
          {socialSending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          {socialSending ? 'Đang gửi...' : 'Gửi MediaSocial'}
        </Button>
      </header>

      <div className="p-3 pb-8">
        {videos.length === 0 ? (
          <p className="py-8 text-center text-xs text-cyber-text-muted">
            {creatorCrawlHash && status === 'running'
              ? 'Đang crawl kênh nhà sáng tạo. Video sẽ tự hiện ở đây khi được tìm thấy, kể cả khi bạn đóng popup.'
              : status === 'idle' && Number(filters.minDuration) > 0
              ? 'Không có video phù hợp. Hãy giảm “Video dài hơn” về 0 phút rồi crawl lại.'
              : 'Hãy crawl theo từ khóa, sau đó kết quả sẽ xuất hiện tại đây để lọc và xem trước.'}
          </p>
        ) : (
          <div className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {videos.map((video) => (
              <article key={video.id} className={`rounded-md border overflow-hidden bg-cyber-bg-tertiary/40 ${selected.has(video.id) ? 'border-cyber-neon-cyan' : 'border-cyber-border-subtle'}`}>
                <div className="relative aspect-video bg-black">
                  {video.preview_url ? (
                    <video
                      controls
                      preload="none"
                      poster={video.cover_url}
                      src={withWorkspace(`/api/videos/stream?platform=${video.platform}&video_id=${encodeURIComponent(video.id)}`)}
                      className="h-full w-full object-cover"
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
                    <span title="Thời lượng"><Clock3 className="inline h-3 w-3" /> {video.duration_seconds ? `${Math.floor(video.duration_seconds / 60)}:${String(video.duration_seconds % 60).padStart(2, '0')}` : '—'}</span>
                    <span title={video.views ? 'Lượt xem' : 'Nền tảng không cung cấp lượt xem'}><Eye className="inline h-3 w-3" /> {video.views ? compact.format(video.views) : '—'}</span>
                    <span><Heart className="inline h-3 w-3" /> {compact.format(video.likes)}</span>
                    <span><MessageCircle className="inline h-3 w-3" /> {compact.format(video.comments)}</span>
                    <span><Share2 className="inline h-3 w-3" /> {compact.format(video.shares)}</span>
                    {video.source_url && <a href={video.source_url} target="_blank" rel="noreferrer" className="ml-auto text-cyber-neon-cyan"><ExternalLink className="h-3 w-3" /></a>}
                  </div>
                  {video.platform === 'dy' && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 w-full text-[10px]"
                      disabled={status !== 'idle' || isStartingCreator}
                      onClick={() => crawlVideoCreator(video)}
                    >
                      {creatorVideoId === video.id ? 'Đang mở kênh...' : 'Xem kênh và crawl tất cả video'}
                    </Button>
                  )}
                </div>
              </article>
            ))}
          </div>
          </div>
        )}
      </div>
      {channelVideo && createPortal(
        <div className="fixed inset-0 z-[110] overflow-y-auto bg-black/70 p-4 backdrop-blur-sm">
          <section className="mx-auto my-6 max-w-6xl overflow-hidden rounded-lg border border-cyber-neon-cyan/60 bg-cyber-bg-tertiary shadow-2xl">
            <header className="flex items-center gap-3 border-b border-cyber-border-subtle p-4">
              <div className="min-w-0">
                <h2 className="text-sm font-bold text-cyber-text-primary">KÊNH CỦA VIDEO</h2>
                <p className="truncate text-[11px] text-cyber-text-muted">{channelVideo.author || channelVideo.title}</p>
              </div>
              <span className="ml-auto text-xs text-cyber-text-muted">
                {channelLoading || (creatorCrawlHash && status === 'running')
                  ? `Đang crawl${channelVideos.length ? ` · đang giữ ${channelVideos.length} video hiện có` : ' / tải danh sách...'}`
                  : `${channelVideos.length} video`}
              </span>
              <Button size="sm" variant="outline" onClick={closeCreatorPopup}>Đóng</Button>
            </header>
            {channelVideos.length > 0 && (
              <div className="space-y-3 border-b border-cyber-border-subtle p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={batchRunning}
                    onClick={() => setChannelSelected(
                      channelSelected.size === channelVideos.filter((video) => video.preview_url).length
                        ? new Set()
                        : new Set(channelVideos.filter((video) => video.preview_url).map((video) => video.id)),
                    )}
                  >
                    {channelSelected.size === channelVideos.filter((video) => video.preview_url).length ? 'Bỏ chọn tất cả' : 'Chọn tất cả'}
                  </Button>
                  <span className="text-xs text-cyber-text-muted">Đã chọn {channelSelected.size} video có thể lồng tiếng</span>
                  <Select value={voice} onValueChange={setVoice} disabled={batchRunning}>
                    <SelectTrigger className="h-8 w-44 text-xs"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="vi-VN-HoaiMyNeural">Hoài My</SelectItem>
                      <SelectItem value="vi-VN-NamMinhNeural">Nam Minh</SelectItem>
                    </SelectContent>
                  </Select>
                  <Select value={mood} onValueChange={setMood} disabled={batchRunning}>
                    <SelectTrigger className="h-8 w-36 text-xs"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="natural">Tự nhiên</SelectItem>
                      <SelectItem value="energetic">Năng lượng</SelectItem>
                      <SelectItem value="warm">Ấm áp</SelectItem>
                    </SelectContent>
                  </Select>
                  <Select value={translationStyle} onValueChange={setTranslationStyle} disabled={batchRunning}>
                    <SelectTrigger className="h-8 w-44 text-xs"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="faithful">Dịch sát nghĩa</SelectItem>
                      <SelectItem value="conversational">Đời thường</SelectItem>
                      <SelectItem value="narration">Thuyết minh mượt</SelectItem>
                      <SelectItem value="review">Review năng lượng</SelectItem>
                    </SelectContent>
                  </Select>
                  <Button
                    size="sm"
                    variant={autoDownloadOnComplete ? 'default' : 'outline'}
                    disabled={batchRunning}
                    onClick={toggleAutoDownload}
                    title="Tự tải file về máy khi từng video lồng tiếng xong"
                  >
                    <Download className="h-4 w-4" /> Tự tải: {autoDownloadOnComplete ? 'Bật' : 'Tắt'}
                  </Button>
                  <Select value={String(videoParallelism)} onValueChange={updateVideoParallelism} disabled={batchRunning}>
                    <SelectTrigger className="h-8 w-36 text-xs" title="Số video lồng tiếng cùng lúc"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {[1, 2, 3, 4].map((count) => <SelectItem key={count} value={String(count)}>{count} video song song</SelectItem>)}
                    </SelectContent>
                  </Select>
                  <Button size="sm" onClick={() => void runBatchDubbing()} disabled={!channelSelected.size || batchRunning}>
                    {batchRunning ? <Loader2 className="h-4 w-4 animate-spin" /> : <Languages className="h-4 w-4" />}
                    {batchRunning ? 'Đang lồng tiếng...' : `Lồng tiếng ${channelSelected.size} video`}
                  </Button>
                </div>
                {batchRunning && (
                  <p className="text-xs text-cyber-text-muted">
                    Đang chạy tối đa {videoParallelism} video độc lập cùng lúc. Mỗi video có job và thư mục riêng; không tải lại trang trước khi lô hoàn tất.
                  </p>
                )}
                {autoDownloadOnComplete && (
                  <p className="text-xs text-cyber-text-muted">Mỗi video sẽ tải về ngay khi xong. Nếu trình duyệt hỏi, hãy cho phép tải nhiều tệp.</p>
                )}
              </div>
            )}
            {channelError ? (
              <p className="p-6 text-sm text-red-300">{channelError}</p>
            ) : captchaRequired && creatorCrawlHash && status === 'running' ? (
              <div className="m-4 rounded-md border border-amber-400/40 bg-amber-400/10 p-4 text-sm text-amber-100">
                Douyin đang yêu cầu xác minh CAPTCHA. Hãy hoàn tất trên cửa sổ Chromium vừa mở; crawler sẽ tiếp tục tự động sau khi xác minh.
              </div>
            ) : (channelLoading || (creatorCrawlHash && status === 'running')) && channelVideos.length === 0 ? (
              <div className="flex items-center justify-center gap-2 p-12 text-sm text-cyber-text-muted">
                <Loader2 className="h-4 w-4 animate-spin" /> Đang crawl kênh. Video sẽ xuất hiện tại đây khi crawler tìm thấy và lưu được video.
              </div>
            ) : channelVideos.length === 0 ? (
              <p className="p-8 text-center text-sm text-cyber-text-muted">Chưa tìm thấy video nào của kênh này.</p>
            ) : (
              <div className="space-y-4 p-4">
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
                {channelVideos.map((video) => (
                  <article key={video.id} className="overflow-hidden rounded-md border border-cyber-border-subtle bg-black/20">
                    <div className="relative aspect-video bg-black">
                      {video.preview_url ? (
                        <video controls preload="none" poster={video.cover_url}
                          src={withWorkspace(`/api/videos/stream?platform=dy&video_id=${encodeURIComponent(video.id)}`)}
                          className="h-full w-full object-cover" />
                      ) : video.cover_url ? <img src={video.cover_url} alt="" className="h-full w-full object-cover" /> : null}
                      <input
                        type="checkbox"
                        aria-label={`Chọn video ${video.title}`}
                        checked={channelSelected.has(video.id)}
                        disabled={!video.preview_url || batchRunning}
                        onChange={() => toggleChannelVideo(video.id)}
                        className="absolute left-2 top-2 h-5 w-5 accent-cyan-400"
                      />
                    </div>
                    <div className="space-y-1 p-2">
                      <p className="line-clamp-2 text-xs text-cyber-text-primary">{video.title}</p>
                      <p className="text-[10px] text-cyber-text-muted">{video.published_at ? new Date(video.published_at * 1000).toLocaleDateString('vi-VN') : 'Ngày đăng không có sẵn'} · {compact.format(video.likes)} lượt thích</p>
                      {batchStatus[video.id] && (
                        <div className={`space-y-1 text-[10px] ${batchStatus[video.id].status === 'error' ? 'text-red-300' : batchStatus[video.id].status === 'done' ? 'text-emerald-300' : 'text-cyan-200'}`}>
                          <p>
                            {batchStatus[video.id].status === 'done' && <CheckCircle2 className="mr-1 inline h-3 w-3" />}
                            {batchStatus[video.id].status === 'error' ? 'LỖI: ' : ''}{batchStatus[video.id].message}
                            {batchStatus[video.id].progress !== undefined && batchStatus[video.id].status !== 'done' && ` · ${Math.round(batchStatus[video.id].progress!)}%`}
                          </p>
                          {(batchStatus[video.id].status === 'preparing' || batchStatus[video.id].status === 'rendering') && batchStatus[video.id].checkedAt && (
                            <p className="text-cyber-text-muted">Server phản hồi lúc {new Date(batchStatus[video.id].checkedAt!).toLocaleTimeString('vi-VN')}</p>
                          )}
                          {batchStatus[video.id].detail && batchStatus[video.id].status !== 'done' && (
                            <p className="line-clamp-2 text-cyber-text-muted" title={batchStatus[video.id].detail}>{batchStatus[video.id].detail}</p>
                          )}
                          {(batchStatus[video.id].status === 'preparing' || batchStatus[video.id].status === 'rendering') && batchStatus[video.id].stageSince && Date.now() - batchStatus[video.id].stageSince! > 120_000 && (
                            <p className="text-amber-300">Công đoạn chưa đổi hơn 2 phút; có thể Whisper hoặc dịch vụ ngoài đang xử lý lâu.</p>
                          )}
                          {batchStatus[video.id].progress !== undefined && batchStatus[video.id].status !== 'done' && (
                            <div className="h-1 overflow-hidden rounded bg-black/30" role="progressbar" aria-valuenow={batchStatus[video.id].progress} aria-valuemin={0} aria-valuemax={100}>
                              <div className="h-full bg-cyan-300 transition-all" style={{ width: `${Math.max(0, Math.min(100, batchStatus[video.id].progress!))}%` }} />
                            </div>
                          )}
                        </div>
                      )}
                      {batchStatus[video.id]?.status === 'done' || video.dubbed_available ? (
                        <a
                          href={withWorkspace(`/api/videos/dub/output/${encodeURIComponent(video.id)}`)}
                          download
                          className="flex h-8 w-full items-center justify-center gap-2 rounded-md border border-emerald-400/50 text-[11px] text-emerald-200 hover:bg-emerald-400/10"
                        >
                          <Download className="h-3.5 w-3.5" /> Tải video lồng tiếng
                        </a>
                      ) : null}
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-8 w-full text-[11px]"
                        disabled={!video.preview_url || dubbing || batchRunning}
                        onClick={() => void prepareDubbing(video)}
                        title={!video.preview_url ? 'Video chưa có URL tải để lồng tiếng' : 'Tạo video lồng tiếng Việt'}
                      >
                        <Languages className="h-3.5 w-3.5" /> Lồng tiếng video này
                      </Button>
                    </div>
                  </article>
                ))}
                </div>
              </div>
            )}
          </section>
        </div>,
        document.body,
      )}
      {dubbingOpen && createPortal(
        <div className="fixed inset-0 z-[100] bg-black/60 p-4 backdrop-blur-sm">
        <div className="mx-auto flex h-full max-w-6xl flex-col overflow-hidden rounded-lg border border-cyber-neon-cyan/60 shadow-2xl bg-cyber-bg-tertiary">
          <div className="flex shrink-0 items-center gap-2 border-b border-cyber-border-subtle p-4">
            <Languages className="h-4 w-4 text-cyber-neon-cyan" />
            <strong className="text-xs text-cyber-text-primary">GIAO DIỆN LỒNG TIẾNG TRUNG → VIỆT {dubVideo ? `— ${dubVideo.id}` : ''}</strong>
            <button className="ml-auto text-xs text-cyber-text-muted" onClick={() => setDubbingOpen(false)}>Đóng</button>
          </div>
          <div className="shrink-0 space-y-3 border-b border-cyber-border-subtle p-4">
          <div className="flex items-center gap-2 rounded border border-cyber-border-subtle bg-black/30 p-3 text-xs">
            {dubError ? <AlertCircle className="h-4 w-4 text-red-400" /> : dubOutput ? <CheckCircle2 className="h-4 w-4 text-green-400" /> : <Loader2 className="h-4 w-4 animate-spin text-cyber-neon-cyan" />}
            <span className={dubError ? 'text-red-300' : 'text-cyber-text-primary'}>{dubStage || 'Đang khởi tạo pipeline...'}</span>
          </div>
          {dubbing && <div className="h-1.5 overflow-hidden rounded bg-black/50"><div className="h-full rounded bg-cyber-neon-cyan transition-all duration-500" style={{ width: `${Math.max(4, dubProgress)}%` }} /></div>}
          </div>
          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
          {dubLogs.length > 0 && (
            <div className="rounded border border-cyber-border-subtle bg-black/50 p-3">
              <div className="mb-2 flex items-center justify-between text-[11px] text-cyber-text-muted">
                <span>NHẬT KÝ DỊCH / TTS / GHÉP VIDEO</span>
                <span>{dubProgress}%</span>
              </div>
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-5 text-cyber-text-primary">
                {dubLogs.join('\n')}
              </pre>
            </div>
          )}
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
              <Select value={mood} onValueChange={setMood}>
                <SelectTrigger className="h-8 w-44 text-xs"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="natural">Tự nhiên, có nhịp nghỉ</SelectItem>
                  <SelectItem value="energetic">Tươi hơn, không quá nhanh</SelectItem>
                  <SelectItem value="warm">Ấm áp, chậm rãi</SelectItem>
                </SelectContent>
              </Select>
              <Select value={translationStyle} onValueChange={setTranslationStyle}>
                <SelectTrigger className="h-8 w-48 text-xs"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="faithful">Sát nghĩa 1–1</SelectItem>
                  <SelectItem value="conversational">Đời thường</SelectItem>
                  <SelectItem value="narration">Thuyết minh mượt</SelectItem>
                  <SelectItem value="review">Review năng lượng</SelectItem>
                </SelectContent>
              </Select>
              <Button size="sm" onClick={renderDubbing} disabled={dubbing || !transcript.trim()}>
                <Languages className="h-4 w-4" /> {dubbing ? 'Đang dịch và ghép...' : 'Transcript đúng — tiến hành lồng tiếng'}
              </Button>
              <Button
                size="sm"
                variant={autoDownloadOnComplete ? 'default' : 'outline'}
                onClick={toggleAutoDownload}
                title="Tự tải file về máy khi lồng tiếng xong"
              >
                <Download className="h-4 w-4" /> Tự tải: {autoDownloadOnComplete ? 'Bật' : 'Tắt'}
              </Button>
              {dubOutput && <a href={withWorkspace(dubOutput)} download className="text-xs text-cyber-neon-cyan underline">Tải output_vi.mp4</a>}
              {(dubbing || dubError) && (
                <div className={`w-full rounded border p-3 ${dubError ? 'border-red-400/50 bg-red-950/20' : 'border-cyber-neon-cyan/50 bg-black/40'}`} aria-live="polite">
                  <div className="mb-2 flex items-center gap-2 text-xs">
                    {dubError ? <AlertCircle className="h-4 w-4 shrink-0 text-red-400" /> : <Loader2 className="h-4 w-4 shrink-0 animate-spin text-cyber-neon-cyan" />}
                    <span className={dubError ? 'text-red-300' : 'text-cyber-text-primary'}>{dubError || dubStage}</span>
                    <strong className="ml-auto text-cyber-neon-cyan">{dubProgress}%</strong>
                  </div>
                  <div className="h-2 overflow-hidden rounded bg-black/60">
                    <div
                      className={`h-full rounded transition-all duration-500 ${dubError ? 'bg-red-400' : 'bg-cyber-neon-cyan'}`}
                      style={{ width: `${Math.max(dubError ? 100 : 3, dubProgress)}%` }}
                    />
                  </div>
                  {dubLogs.length > 0 && <p className="mt-2 truncate font-mono text-[10px] text-cyber-text-muted">{dubLogs[dubLogs.length - 1]}</p>}
                </div>
              )}
            </div>
          </>}
          </div>
        </div>
        </div>
      , document.body)}
    </section>
  )
}
