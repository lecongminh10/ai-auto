import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import LanguageDetector from 'i18next-browser-languagedetector'

// 中文翻译
import zhCommon from './locales/zh-CN/common.json'
import zhConfig from './locales/zh-CN/config.json'
import zhTerminal from './locales/zh-CN/terminal.json'
import zhEnv from './locales/zh-CN/env.json'

// 英文翻译
import enCommon from './locales/en-US/common.json'
import enConfig from './locales/en-US/config.json'
import enTerminal from './locales/en-US/terminal.json'
import enEnv from './locales/en-US/env.json'

// Bản dịch tiếng Việt
import viCommon from './locales/vi-VN/common.json'
import viConfig from './locales/vi-VN/config.json'
import viTerminal from './locales/vi-VN/terminal.json'
import viEnv from './locales/vi-VN/env.json'

// Chuyển người dùng hiện tại sang tiếng Việt một lần sau khi bổ sung bản dịch.
const VIETNAMESE_MIGRATION_KEY = 'mediacrawler_vietnamese_locale_v1'
if (!localStorage.getItem(VIETNAMESE_MIGRATION_KEY)) {
  localStorage.setItem('mediacrawler_language', 'vi-VN')
  localStorage.setItem(VIETNAMESE_MIGRATION_KEY, 'true')
}

const resources = {
  'vi-VN': {
    common: viCommon,
    config: viConfig,
    terminal: viTerminal,
    env: viEnv,
  },
  'zh-CN': {
    common: zhCommon,
    config: zhConfig,
    terminal: zhTerminal,
    env: zhEnv,
  },
  'en-US': {
    common: enCommon,
    config: enConfig,
    terminal: enTerminal,
    env: enEnv,
  },
}

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
    fallbackLng: 'vi-VN',
    lng: localStorage.getItem('mediacrawler_language') || 'vi-VN',
    defaultNS: 'common',
    interpolation: {
      escapeValue: false,
    },
    detection: {
      order: ['localStorage', 'navigator'],
      caches: ['localStorage'],
      lookupLocalStorage: 'mediacrawler_language',
    },
  })

export default i18n
