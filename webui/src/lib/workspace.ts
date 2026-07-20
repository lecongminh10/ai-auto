const STORAGE_KEY = 'mediacrawler_workspace_id'

function createWorkspaceId(): string {
  const random = typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID().replace(/-/g, '')
    : `${Date.now()}${Math.random().toString(36).slice(2)}`
  return `ws_${random}`.slice(0, 64)
}

export const workspaceId = (() => {
  const existing = localStorage.getItem(STORAGE_KEY)
  if (existing && /^[A-Za-z0-9_-]{8,64}$/.test(existing)) return existing
  const created = createWorkspaceId()
  localStorage.setItem(STORAGE_KEY, created)
  return created
})()

export function workspaceFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const headers = new Headers(init.headers)
  headers.set('X-Workspace-ID', workspaceId)
  return fetch(input, { ...init, headers })
}

export function withWorkspace(url: string): string {
  const parsed = new URL(url, window.location.origin)
  parsed.searchParams.set('workspace_id', workspaceId)
  return `${parsed.pathname}${parsed.search}${parsed.hash}`
}
