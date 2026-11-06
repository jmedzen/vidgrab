import { useEffect, useState } from 'react'
import ItemCard from './ItemCard'
import type { ItemProgress } from './ItemCard'
import { LIC_CLASS, LIC_LABEL } from '../lib/format'
import type { Project } from '../types'

export type ClipMode = 'auto' | 'range' | 'full'

export interface GrabOptions {
  mode: ClipMode
  per: number
  starts: string
  ranges: string
  height: number
  frames: boolean
  audio: boolean
  saveSettings: boolean
}

interface Props {
  project: Project
  selected: ReadonlySet<number>
  options: GrabOptions
  onOptions: (patch: Partial<GrabOptions>) => void
  onToggle: (index: number) => void
  onSelectAll: (on: boolean) => void
  onPlay: (rel: string, name: string) => void
  onLightbox: (kind: 'img' | 'video', src: string) => void
  progress?: Record<number, ItemProgress>
  onGrab: () => void
  onRefresh: () => void
  onOpenList: () => void
  busy: boolean
}

export default function ProjectView({
  project, selected, options, onOptions, onToggle, onSelectAll,
  onPlay, onLightbox, onGrab, onRefresh, onOpenList, busy, progress,
}: Props) {
  const items = project.items ?? []
  const stats = project.stats ?? {}
  const byLicence = (stats.by_license ?? {}) as Record<string, number>

  const entries = Object.entries(byLicence)
  const known = entries.filter(([k]) => k !== 'unknown' && k !== 'none')
  const unknownCount = entries
    .filter(([k]) => k === 'unknown' || k === 'none')
    .reduce((sum, [, v]) => sum + v, 0)

  const allOn = items.length > 0 && items.every((i) => selected.has(i.index))

  // 只在「滾離頂端」之後才浮出來。
  // 一直掛著會擋住列表右側的按鈕（實際擋過），
  // 而滾回頂端時上面那排本來就看得見，不需要 pill。
  const [scrolled, setScrolled] = useState(false)
  useEffect(() => {
    // 真正在捲的是 .mainscroll（main 裡那層負責 overflow）。
    // 監聽 window 或 main 都永遠是 0 —— 實際踩過兩次。
    const host = document.querySelector('.mainscroll') ?? document.documentElement
    const onScroll = () => setScrolled(host.scrollTop > 160)
    onScroll()
    host.addEventListener('scroll', onScroll, { passive: true })
    return () => host.removeEventListener('scroll', onScroll)
  }, [])

  return (
    <>
      <header className="bar">
        <h2>{(project.queries ?? []).join('、')}</h2>
        <span className="sub" style={{ margin: 0 }}>
          {items.length} 支候選 · {String(stats.with_clips ?? 0)} 段已切 · {project.clips_bytes}
        </span>
      </header>

      <div className="pills">
        <span className="pill">{(project.sources ?? []).join(' + ')}</span>
        {known.map(([k, v]) => (
          <span key={k} className={`pill ${LIC_CLASS[k] || 'lic-unknown'}`}>
            {LIC_LABEL[k] ?? k} · {v}
          </span>
        ))}
        {unknownCount
          ? <span className="licnote">{unknownCount} 支未標示授權</span>
          : null}
      </div>

      <div className="toolbar">
        <label style={labelStyle}>
          <input type="checkbox" id="selall" checked={allOn}
                 onChange={(e) => onSelectAll(e.target.checked)} /> 全選
        </label>
        <span className="sp" />

        <select id="mode" aria-label="切片方式" style={{ width: 132 }}
                value={options.mode}
                onChange={(e) => onOptions({ mode: e.target.value as ClipMode })}>
          <option value="auto">切片（每段固定）</option>
          <option value="range">切片（自訂區段）</option>
          <option value="full">整支下載</option>
        </select>

        {options.mode !== 'full' && (
          <>
            <span className="sub" id="perlabel" style={{ margin: 0 }}>每段</span>
            <input type="number" id="per" value={options.per} min={1} step={1}
                   aria-label="每段秒數" style={{ width: 66 }}
                   onChange={(e) => onOptions({ per: Number(e.target.value) || 15 })} />
            <span className="sub" id="persuffix" style={{ margin: 0 }}>秒</span>
          </>
        )}

        {options.mode === 'auto' && (
          <input type="text" id="starts" placeholder="指定秒數，如 90, 3:20"
                 style={{ width: 170 }} value={options.starts}
                 onChange={(e) => onOptions({ starts: e.target.value })} />
        )}
        {options.mode === 'range' && (
          <input type="text" id="ranges" placeholder="區段，如 1:00-2:30, 5:00-6:00"
                 style={{ width: 230 }} value={options.ranges}
                 onChange={(e) => onOptions({ ranges: e.target.value })} />
        )}

        <select id="height" aria-label="解析度" style={{ width: 104 }}
                value={options.height}
                onChange={(e) => onOptions({ height: Number(e.target.value) })}>
          <option value={2160}>4K</option>
          <option value={1440}>1440p</option>
          <option value={1080}>1080p</option>
          <option value={720}>720p</option>
        </select>

        <label style={labelStyle}>
          <input type="checkbox" id="frames" checked={options.frames}
                 onChange={(e) => onOptions({ frames: e.target.checked })} /> 抓畫面
        </label>
        <label style={labelStyle}>
          <input type="checkbox" id="audio" checked={options.audio}
                 onChange={(e) => onOptions({ audio: e.target.checked })} /> 只要聲音
        </label>
        <label style={labelStyle} title="勾起來：這次用的下載參數會存成預設值">
          <input type="checkbox" id="savesettings" checked={options.saveSettings}
                 onChange={(e) => onOptions({ saveSettings: e.target.checked })} /> 記住這些設定
        </label>

        <button className="primary" id="grab"
                style={{ width: 'auto', padding: '8px 18px' }}
                disabled={busy} onClick={onGrab}>切片段</button>
        <button className="ghost tiny" id="refresh" disabled={busy} onClick={onRefresh}>
          重新確認授權
        </button>
        <button className="ghost tiny" id="openlist" onClick={onOpenList}>開 HTML 清單</button>
      </div>

      {/* 浮動操作列：長列表往下滾之後，原本最上面那排按鈕就看不到了，
          得一路滾回頂端才能動手。這裡常駐在右下角，勾選數量跟著變。
          pointer-events 只開在 pill 本身，不要擋住底下的內容。 */}
      {items.length > 0 ? (
        <div id="actionpill" role="toolbar" aria-label="批次操作"
             data-visible={scrolled ? '1' : '0'}>
          <span className="ap-count">
            {selected.size > 0 ? `已選 ${selected.size} 支` : '還沒勾選'}
          </span>
          <button className="primary" id="pillgrab"
                  disabled={busy || selected.size === 0}
                  onClick={onGrab}>
            {busy ? '處理中…' : '切片段'}
          </button>
          <button className="ghost tiny" id="pillrefresh" disabled={busy} onClick={onRefresh}>
            確認授權
          </button>
          <button className="ghost tiny" id="pilltop" aria-label="回到頂端"
                  onClick={() => {
                    const host = document.querySelector('.mainscroll')
                    if (host) host.scrollTo({ top: 0, behavior: 'smooth' })
                    else window.scrollTo({ top: 0, behavior: 'smooth' })
                  }}>
            ↑ 頂端
          </button>
        </div>
      ) : null}

      {items.length === 0
        ? <div className="empty">這個專案沒有結果。</div>
        : items.map((it) => (
          <ItemCard
            key={it.index}
            item={it}
            selected={selected.has(it.index)}
            onToggle={onToggle}
            onPlay={onPlay}
            onLightbox={onLightbox}
            progress={progress?.[it.index]}
          />
        ))}
    </>
  )
}

const labelStyle = {
  display: 'flex', gap: 6, alignItems: 'center',
  fontSize: 12.5, color: 'var(--muted)',
} as const
