export interface CrawlerConfig {
  platform: string
  login_type: string
  crawler_type: string
  keywords: string
  start_page: number
  enable_comments: boolean
  enable_sub_comments: boolean
  save_option: string
  cookies: string
  headless: boolean
  min_video_duration_seconds: number
}

export interface CrawlerStatus {
  status: 'idle' | 'running' | 'stopping' | 'error'
  platform: string | null
  crawler_type: string | null
  started_at: string | null
  error_message: string | null
}

export interface LogEntry {
  id: number
  timestamp: string
  level: 'info' | 'warning' | 'error' | 'success' | 'debug'
  message: string
}

export interface DataFile {
  name: string
  path: string
  size: number
  modified_at: number
  record_count: number | null
  type: string
}

export interface Platform {
  value: string
  label: string
  icon: string
}

export interface ConfigOption {
  value: string
  label: string
}
