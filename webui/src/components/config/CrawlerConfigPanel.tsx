import type { ComponentType, ReactNode } from 'react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Globe, KeyRound, Play, Square } from 'lucide-react'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { useCrawlerStore } from '@/store/crawlerStore'
import { usePlatforms, useConfigOptions, useStartCrawler, useStopCrawler } from '@/hooks/useCrawler'

type SectionProps = {
  title: string
  description: string
  icon: ComponentType<{ className?: string }>
  children: ReactNode
  className?: string
}

function Section({ title, description, icon: Icon, children, className = '' }: SectionProps) {
  return (
    <section className={`rounded-lg glass-panel float-panel overflow-hidden ${className}`}>
      <header className="px-4 py-3 border-b border-cyber-border-subtle/50 flex items-center gap-3 bg-cyber-bg-tertiary/30">
        <div className="h-8 w-8 rounded-md bg-cyber-bg-tertiary border border-cyber-border-subtle flex items-center justify-center flex-shrink-0">
          <Icon className="h-4 w-4 text-cyber-neon-cyan" />
        </div>
        <div className="min-w-0">
          <div className="text-xs font-mono font-semibold text-cyber-text-primary tracking-wide">
            {title}
          </div>
          <div className="text-[10px] text-cyber-text-muted leading-snug truncate">
            {description}
          </div>
        </div>
      </header>
      <div className="p-4 space-y-4">
        {children}
      </div>
    </section>
  )
}

type FieldProps = {
  label: string
  hint?: string
  children: ReactNode
}

function Field({ label, hint, children }: FieldProps) {
  return (
    <div className="space-y-2">
      <div className="space-y-0.5">
        <Label className="text-xs text-cyber-text-secondary font-mono">
          {label}
        </Label>
        {hint ? (
          <p className="text-[10px] text-cyber-text-muted leading-snug">
            {hint}
          </p>
        ) : null}
      </div>
      {children}
    </div>
  )
}

type KeywordInputProps = {
  value: string
  onChange: (value: string) => void
  placeholder?: string
  disabled?: boolean
}

function KeywordInput({ value, onChange, placeholder, disabled }: KeywordInputProps) {
  return (
    <Input
      value={value}
      onChange={(event) => onChange(event.target.value)}
      placeholder={placeholder}
      disabled={disabled}
      className="h-9 text-xs"
    />
  )
}

export function CrawlerConfigPanel() {
  const { t } = useTranslation('config')
  const config = useCrawlerStore((state) => state.config)
  const updateConfig = useCrawlerStore((state) => state.updateConfig)
  const status = useCrawlerStore((state) => state.status)

  const { data: platforms } = usePlatforms()
  const { data: options } = useConfigOptions()
  const { mutate: startCrawler, isPending: isStarting } = useStartCrawler()
  const { mutate: stopCrawler, isPending: isStopping } = useStopCrawler()
  const [isTranslating, setIsTranslating] = useState(false)
  const [translatedKeywords, setTranslatedKeywords] = useState('')

  const isDisabled = status === 'running' || status === 'stopping'
  const isRunning = status === 'running'
  const isBusy = isStarting || isStopping || isTranslating || status === 'stopping'

  const handleStart = async () => {
    setIsTranslating(true)
    try {
      const response = await fetch('/api/videos/translate-keywords', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ keywords: config.keywords }),
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Không thể dịch từ khóa')
      setTranslatedKeywords(payload.crawler_keywords)
      startCrawler({ ...config, crawler_type: 'search', keywords: payload.crawler_keywords })
    } catch (error) {
      alert(error instanceof Error ? error.message : 'Không thể dịch từ khóa sang tiếng Trung')
    } finally {
      setIsTranslating(false)
    }
  }

  const handleStop = () => {
    stopCrawler()
  }

  return (
    <div className="space-y-4 animate-slide-up">
      {/* Cấu hình tải video */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Column 1: Target & Mode Section */}
        <Section
          title={t('section.targetMatrix.title')}
          description={t('section.targetMatrix.description')}
          icon={Globe}
        >
          <Field label={t('field.platform')}>
            <Select
              value={config.platform}
              onValueChange={(value) => updateConfig({ platform: value })}
              disabled={isDisabled}
            >
              <SelectTrigger className="h-9 text-xs">
                <SelectValue placeholder={t('field.platformPlaceholder')} />
              </SelectTrigger>
              <SelectContent>
                {platforms?.map((platform) => (
                  <SelectItem key={platform.value} value={platform.value}>
                    {platform.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <Field label={t('field.keywords')} hint={t('field.keywordsHint')}>
            <KeywordInput
              placeholder={t('field.keywordsPlaceholder')}
              value={config.keywords}
              onChange={(keywords) => {
                updateConfig({ keywords, crawler_type: 'search' })
                setTranslatedKeywords('')
              }}
              disabled={isDisabled}
            />
            {translatedKeywords && (
              <p className="text-[11px] font-mono text-cyber-neon-green">
                Từ khóa gửi sang nền tảng: {translatedKeywords}
              </p>
            )}
          </Field>
        </Section>

        {/* Column 2: Authentication Section */}
        <Section
          title={t('section.authMatrix.title')}
          description={t('section.authMatrix.description')}
          icon={KeyRound}
        >
          <Field label={t('field.loginMethod')}>
            <Select
              value={config.login_type}
              onValueChange={(value) => updateConfig({ login_type: value })}
              disabled={isDisabled}
            >
              <SelectTrigger className="h-9 text-xs">
                <SelectValue placeholder={t('field.loginMethodPlaceholder')} />
              </SelectTrigger>
              <SelectContent>
                {options?.login_types.map((type) => (
                  <SelectItem key={type.value} value={type.value}>
                    {type.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          {config.login_type === 'cookie' ? (
            <Field label={t('field.cookies')} hint={t('field.cookiesHint')}>
              <textarea
                value={config.cookies}
                onChange={(e) => updateConfig({ cookies: e.target.value })}
                disabled={isDisabled}
                placeholder={t('field.cookiesPlaceholder')}
                className="min-h-[80px] w-full rounded-md border border-cyber-border-DEFAULT bg-cyber-bg-tertiary px-3 py-2 text-xs font-mono text-cyber-text-primary placeholder:text-cyber-text-muted focus-visible:outline-none focus-visible:border-cyber-neon-cyan/50 focus-visible:shadow-cyber-soft disabled:cursor-not-allowed disabled:opacity-50 transition-all resize-none"
              />
            </Field>
          ) : null}

          {config.login_type === 'cookie' && (config.platform === 'xhs' || config.platform === 'dy') ? (
            <div className="rounded-lg border border-cyber-neon-orange/30 bg-cyber-neon-orange/5 p-3 text-[11px] leading-snug text-cyber-neon-orange font-mono">
              {t('warning.cookieSlider')}
            </div>
          ) : null}
        </Section>
      </div>

      {/* Row 2: Start/Stop Button - Full Width */}
      <div className="w-full">
        {isRunning ? (
          <Button
            onClick={handleStop}
            disabled={isBusy || !config.keywords.trim()}
            className="w-full h-12 bg-cyber-neon-pink text-white font-mono font-bold text-sm tracking-wider hover:bg-cyber-neon-pink/90 hover:shadow-glow-pink-sm transition-all"
          >
            <Square className="w-4 h-4" />
            {isStopping ? t('button.stopping') : t('button.terminate')}
          </Button>
        ) : (
          <Button
            onClick={handleStart}
            disabled={isBusy}
            className="w-full h-12 bg-cyber-neon-cyan text-cyber-bg-primary font-mono font-bold text-sm tracking-wider hover:bg-cyber-neon-cyan/90 hover:shadow-glow-cyan-sm transition-all"
          >
            <Play className="w-4 h-4" />
            {isTranslating ? 'ĐANG DỊCH TỪ KHÓA...' : isStarting ? t('button.initiating') : t('button.initiateScan')}
          </Button>
        )}
      </div>
    </div>
  )
}
