// 後端 API 的契約。欄位一律以實際 payload 為準，不是憑印象寫的。

export interface Clip {
  name: string
  rel: string
  url: string
  start: number
  end: number
  full: boolean
  span: string
  size: string
  dims: string
  /** 片段自己的縮圖（從這個片段檔抽的）；沒有就是空字串 */
  thumb: string
  /** 'portrait' | 'landscape' | '' */
  orientation: string
}

export interface Item {
  index: number
  platform: string
  platform_label: string
  vid: string
  title: string
  author: string
  duration: string
  duration_sec: number
  views: number
  views_label: string
  pubdate: string
  url: string
  license_kind: string
  license_label: string
  license_short: string
  repost: boolean
  error: string
  note: string
  keyword: string
  thumbs: string[]
  clips: Clip[]
}

export interface ProjectStats {
  candidates?: number
  clips?: number
  [k: string]: unknown
}

export interface Project {
  name: string
  path: string
  queries: string[]
  sources: string[]
  created: string
  updated: string
  stats: ProjectStats
  clips_bytes: number
  items: Item[]
}

export interface LibraryProject {
  name: string
  title: string
  pinned: boolean
  note: string
  missing: boolean
  queries: string[]
  count: number
  clips: number
  created: string
  size: string
}

export interface LibraryState {
  ok: boolean
  projects: LibraryProject[]
  order: string[]
}

export interface VersionInfo {
  version: string
  build: string
  full: string
}

export type ThemeKind = 'dark' | 'light'

export interface ThemeDef {
  id: string
  name: string
  kind: ThemeKind
  dot: string
  accent: string
}

/** 下載進度事件（走 SSE 的 payload） */
export interface ProgressPayload {
  kind: 'download'
  index: number
  position: number
  total: number
  title: string
  asset_id: string
  percent: number
  estimated?: boolean
  speed?: string | null
  eta?: string | null
  fragment?: string | null
  start?: number | null
  end?: number | null
}
