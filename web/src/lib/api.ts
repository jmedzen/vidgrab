import type {
  LibraryState, Project, VersionInfo,
} from '../types'

/** 後端錯誤 envelope：{ ok:false, error:{ code, message, retryable, hint } } */
export class ApiError extends Error {
  readonly code: string
  readonly retryable: boolean
  readonly hint: string

  constructor(message: string, code = '', retryable = false, hint = '') {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.retryable = retryable
    this.hint = hint
  }
}

interface RawError {
  code?: string
  message?: string
  retryable?: boolean
  hint?: string
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  const data: unknown = await res.json().catch(() => ({}))

  if (!res.ok) {
    const body = data as { error?: RawError | string }
    const err = body?.error
    if (err && typeof err === 'object') {
      throw new ApiError(
        err.message || res.statusText,
        err.code || '',
        Boolean(err.retryable),
        err.hint || '',
      )
    }
    throw new ApiError(
      typeof err === 'string' ? err : res.statusText,
      '',
      false,
    )
  }
  return data as T
}

export const api = {
  version: () => request<VersionInfo>('/api/version'),

  /** GET /api/projects 回的是裸陣列，不是 { projects: [...] } */
  projects: () => request<LibraryState['projects']>('/api/projects'),

  library: (payload?: Record<string, unknown>) =>
    request<LibraryState>('/api/library', payload
      ? { method: 'POST', body: JSON.stringify(payload) }
      : undefined),

  project: (name: string) =>
    request<Project>(`/api/project/${encodeURIComponent(name)}`),

  search: (body: Record<string, unknown>) =>
    request<{ job: string }>('/api/search', {
      method: 'POST', body: JSON.stringify(body),
    }),

  refresh: (body: Record<string, unknown>) =>
    request<{ job: string }>('/api/refresh', {
      method: 'POST', body: JSON.stringify(body),
    }),

  openProject: (body: Record<string, unknown>) =>
    request<{ ok: boolean }>('/api/open', {
      method: 'POST', body: JSON.stringify(body),
    }),

  pick: (body: Record<string, unknown>) =>
    request<{ job: string }>('/api/pick', {
      method: 'POST', body: JSON.stringify(body),
    }),

  config: () => request<{ path: string; values: Record<string, unknown> }>('/api/config'),

  setConfig: (set: Record<string, unknown>) =>
    request<{ ok: boolean }>('/api/config', {
      method: 'POST', body: JSON.stringify({ set }),
    }),

  resetConfig: (keys: string[] | null) =>
    request<{ ok: boolean }>('/api/config', {
      method: 'POST', body: JSON.stringify({ reset: keys ?? [] }),
    }),

  scan: () => request<{
    ok: boolean
    root: string
    on_disk: { folder: string }[]
    untracked?: { folder: string }[]
    missing?: { folder: string }[]
  }>('/api/library/scan'),

  loginStatus: () => request<Record<string, unknown>>('/api/login/status'),
  loginStart: () => request<{ key: string; qr: string }>('/api/login/start', { method: 'POST' }),
  loginPoll: (key: string) =>
    request<Record<string, unknown>>(`/api/login/poll?key=${encodeURIComponent(key)}`),
}

/**
 * 舊版的 api() 是全域函式：check_ui.py 會直接呼叫它拿 payload 來驗欄位。
 * React 版保留一個同簽名的窗口，行為跟原本一致（回傳 parsed JSON）。
 */
export async function rawApi(path: string, opts?: RequestInit): Promise<unknown> {
  return request<unknown>(path, opts)
}

export interface JobEvent {
  kind: 'log' | 'progress' | 'done' | 'error'
  text?: string
  payload?: unknown
}

/** 訂閱一個 job 的 SSE 串流。回傳取消函式。 */
export function followJob(
  jobId: string,
  onEvent: (ev: JobEvent) => void,
): () => void {
  const es = new EventSource(`/api/stream/${encodeURIComponent(jobId)}`)

  es.addEventListener('log', (e) => {
    // SSE 的 data 一律是 JSON 字串，直接拿會連引號一起顯示。
    // 後端有時送純字串、有時送物件，兩種都吃。
    const raw = (e as MessageEvent<string>).data
    let text = raw
    try {
      const parsed: unknown = JSON.parse(raw)
      if (typeof parsed === 'string') text = parsed
      else if (parsed && typeof parsed === 'object' && 'text' in parsed) {
        text = String((parsed as { text: unknown }).text)
      }
    } catch {
      /* 不是 JSON 就照原樣用 */
    }
    onEvent({ kind: 'log', text })
  })
  es.addEventListener('progress', (e) => {
    onEvent({ kind: 'progress', payload: safeParse((e as MessageEvent<string>).data) })
  })
  es.addEventListener('done', (e) => {
    onEvent({ kind: 'done', payload: safeParse((e as MessageEvent<string>).data) })
    es.close()
  })
  es.addEventListener('error', () => {
    // EventSource 的 error 有兩種：串流結束、或連線失敗。
    // 已經關掉的不用再報。
    if (es.readyState === EventSource.CLOSED) return
    onEvent({ kind: 'error', text: '串流中斷' })
  })

  return () => es.close()
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}
