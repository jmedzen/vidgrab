import { useEffect, useRef, useState } from 'react'
import type { LibraryProject } from '../types'

export interface ScanResult {
  ok: boolean
  root: string
  on_disk: { folder: string }[]
  untracked?: { folder: string }[]
  missing?: { folder: string }[]
}

interface Props {
  open: boolean
  projects: LibraryProject[]
  currentName: string | null
  sort: string
  onSort: (v: string) => void
  onClose: () => void
  onPick: (name: string) => void
  onRename: (from: string, to: string) => void
  onPin: (name: string) => void
  scan: ScanResult | null
  scanning: boolean
  onScan: () => void
  onReorder: (order: string[]) => void
  onAdopt: (folders: string[]) => void
  onForget: (folders: string[]) => void
}

export default function LibraryPanel({
  open, projects, currentName, sort, onSort, onClose,
  onPick, onRename, onPin, onScan, onReorder, onAdopt, onForget,
  scan, scanning,
}: Props) {
  const [renaming, setRenaming] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  // 改名用非受控 input：受控元件會被「直接設 .value」繞過 onChange，
  // 自動化操作（也包含一些瀏覽器擴充）就會改不動。提交時再讀 ref。
  const draftRef = useRef<HTMLInputElement>(null)
  const [dragFrom, setDragFrom] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  const commit = () => {
    const value = (draftRef.current?.value ?? draft).trim()
    if (renaming && value && value !== renaming) onRename(renaming, value)
    setRenaming(null)
  }

  return (
    <div className={`libmodal${open ? ' show' : ''}`} id="libmodal"
         role="dialog" aria-modal="true"
         aria-labelledby="libtitle"
         onClick={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="libpanel">
        <div className="libhead">
          <h2 id="libtitle">我的素材專案</h2>
          <span className="count" id="libcount">
            {projects.length ? `${projects.length} 個專案` : ''}
          </span>
          <span className="grow" />
          <select id="libsort" aria-label="排序方式" title="排序方式"
                  style={{ width: 150 }} value={sort}
                  onChange={(e) => onSort(e.target.value)}>
            <option value="manual">手動順序</option>
            <option value="created">建立時間</option>
            <option value="name">名稱</option>
            <option value="clips">片段數</option>
            <option value="count">候選數</option>
          </select>
          <button className="ghost tiny" id="libscan"
                  title="掃描 projects/，找出未納入或遺失的專案" onClick={onScan}>
            ⟳ 掃描
          </button>
          <button className="ghost tiny" id="libclose" onClick={onClose}>關閉</button>
        </div>

        <div className="libbody">
          <div id="libscanbox">
            {scanning
              ? <div className="libscan">掃描中…</div>
              : scan
                ? (
                  <div className="libscan" style={scan.ok ? { color: 'var(--muted)' } : undefined}>
                    {scan.ok
                      ? `✓ 掃描完成，記錄與 ${scan.root} 一致（${scan.on_disk.length} 個專案）`
                      : (
                        <>
                          <h4>掃描結果</h4>
                          {(scan.untracked?.length ?? 0) > 0 && (
                            <div>
                              · 磁碟上有、記錄裡沒有：<b>{scan.untracked!.length}</b> 個
                              <div className="row2">
                                {scan.untracked!.map((x) => (
                                  <span className="f" key={x.folder}>{x.folder}</span>
                                ))}
                                <span className="grow" />
                                <button className="mini"
                                        onClick={() => onAdopt(scan.untracked!.map((x) => x.folder))}>
                                  全部納入
                                </button>
                              </div>
                            </div>
                          )}
                          {(scan.missing?.length ?? 0) > 0 && (
                            <div style={{ marginTop: 8 }}>
                              · 記錄有、磁碟上找不到：<b>{scan.missing!.length}</b> 個
                              <span style={{ color: 'var(--muted)' }}>（可能是換了目錄或被移走）</span>
                              <div className="row2">
                                {scan.missing!.map((x) => (
                                  <span className="f" key={x.folder}>{x.folder}</span>
                                ))}
                                <span className="grow" />
                                <button className="mini ghost"
                                        onClick={() => onForget(scan.missing!.map((x) => x.folder))}>
                                  移除這些空記錄
                                </button>
                              </div>
                            </div>
                          )}
                        </>
                      )}
                  </div>
                )
                : null}
          </div>
          {projects.length === 0
            ? (
              <div className="libempty" style={{ gridColumn: '1/-1' }}>
                <div className="big">📁</div>
                還沒有素材專案。<br />在左邊打關鍵字搜一輪，就會出現在這裡。
              </div>
            )
            : (
              <div className="libgrid" id="libgrid">
                {projects.map((p, i) => (
                  <div
                    key={p.name}
                    className={
                      `libcard${currentName === p.name ? ' current' : ''}`
                      + `${p.missing ? ' ismissing' : ''}`
                      + `${dragFrom === p.name ? ' dragging' : ''}`
                    }
                    data-name={p.name}
                    data-i={i}
                    draggable
                    tabIndex={0}
                    onClick={(e) => {
                      if ((e.target as HTMLElement).closest('[data-act]')) return
                      onPick(p.name)
                    }}
                    onDoubleClick={(e) => {
                      if ((e.target as HTMLElement).closest('[data-act]')) return
                      setRenaming(p.name)
                      setDraft(p.title || p.name)
                    }}
                    onDragStart={() => setDragFrom(p.name)}
                    onDragEnd={() => setDragFrom(null)}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={() => {
                      if (!dragFrom || dragFrom === p.name) return
                      const order = projects.map((x) => x.name)
                      const from = order.indexOf(dragFrom)
                      const to = order.indexOf(p.name)
                      if (from < 0 || to < 0) return
                      order.splice(to, 0, order.splice(from, 1)[0]!)
                      onReorder(order)
                      setDragFrom(null)
                    }}
                  >
                    <div className="lctitle">
                      {p.pinned ? <span className="pinmark" title="已釘選">📌</span> : null}
                      {renaming === p.name
                        ? (
                          <input
                            ref={draftRef}
                            className="renaming"
                            autoFocus
                            defaultValue={draft}
                            onChange={(e) => setDraft(e.target.value)}
                            onBlur={commit}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') commit()
                              if (e.key === 'Escape') setRenaming(null)
                            }}
                            onClick={(e) => e.stopPropagation()}
                          />
                        )
                        : <span className="lctext">{p.title || p.name}</span>}
                    </div>
                    <div className="lcmeta">
                      <span>{p.count} 支</span>
                      <span>{p.clips} 段</span>
                      {p.size ? <span>{p.size}</span> : null}
                      {p.created ? <span>{p.created.slice(0, 10)}</span> : null}
                    </div>
                    <div className="lcqueries">
                      {(p.queries ?? []).join('、') || '—'}
                    </div>
                    <div className="lcactions">
                      <button className="mini" data-act="open" onClick={() => onPick(p.name)}>
                        開啟
                      </button>
                      <button className="mini" data-act="rename"
                              onClick={() => { setRenaming(p.name); setDraft(p.title || p.name) }}>
                        改名
                      </button>
                      <button className="mini" data-act="pin" onClick={() => onPin(p.name)}>
                        {p.pinned ? '取消釘選' : '釘選'}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
        </div>
      </div>
    </div>
  )
}
