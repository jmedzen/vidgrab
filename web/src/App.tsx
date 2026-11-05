import { useCallback, useEffect, useRef, useState } from 'react'
import Sidebar, { DEFAULT_PARAMS } from './components/Sidebar'
import type { SearchParams } from './components/Sidebar'
import ProjectView from './components/ProjectView'
import type { GrabOptions } from './components/ProjectView'
import Player from './components/Player'
import ThemePicker from './components/ThemePicker'
import DownloadCard from './components/DownloadCard'
import Console from './components/Console'
import type { LogLine } from './components/Console'
import Toast from './components/Toast'
import LoginModal from './components/LoginModal'
import LibraryPanel from './components/LibraryPanel'
import ProgressBar from './components/ProgressBar'
import AdminPanel from './components/AdminPanel'
import type { ConfigField } from './components/AdminPanel'
import type { ScanResult } from './components/LibraryPanel'
import { api, followJob } from './lib/api'
import type { JobEvent } from './lib/api'
import { applyThemeToDocument, initialTheme, findTheme } from './lib/theme'
import { tsToSec } from './lib/format'
import { installDevBridge } from './lib/devtools'
import { useSidebarResizer } from './lib/useSidebarResizer'
import type { LibraryProject, Project, VersionInfo } from './types'

const DEFAULT_GRAB: GrabOptions = {
  mode: 'auto', per: 15, starts: '', ranges: '',
  height: 1080, frames: true, audio: false, saveSettings: false,
}

interface Download {
  on: boolean
  done: boolean
  title: string
  percent: number
  totalPercent: number
  speed: string
  eta: string
  retry: number
  maxRetries: number
}

const SORT_LABEL: Record<string, string> = {
  manual: '手動順序', created: '建立時間', name: '名稱',
  clips: '片段數', count: '候選數',
}

const NO_DOWNLOAD: Download = {
  on: false, done: false, title: '', percent: 0,
  totalPercent: 0, speed: '', eta: '', retry: 0, maxRetries: 10,
}

export default function App() {
  const [theme, setTheme] = useState(initialTheme)
  const [build, setBuild] = useState<VersionInfo | null>(null)
  const [params, setParams] = useState<SearchParams>(DEFAULT_PARAMS)
  const [grab, setGrab] = useState<GrabOptions>(DEFAULT_GRAB)
  const [project, setProject] = useState<Project | null>(null)
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set())
  const [playing, setPlaying] = useState<{ rel: string; name: string } | null>(null)
  const [lightbox, setLightbox] = useState<{ kind: 'img' | 'video'; src: string } | null>(null)
  const [library, setLibrary] = useState<LibraryProject[]>([])
  const [libOpen, setLibOpen] = useState(false)
  const [libSort, setLibSort] = useState('manual')
  const [scan, setScan] = useState<ScanResult | null>(null)
  const [scanning, setScanning] = useState(false)
  const [adminOpen, setAdminOpen] = useState(false)
  const [config, setConfig] = useState<{ path: string; fields: ConfigField[] } | null>(null)
  const [busy, setBusy] = useState('')
  const [download, setDownload] = useState<Download>(NO_DOWNLOAD)
  const [lines, setLines] = useState<LogLine[]>([])
  const [toastMsg, setToastMsg] = useState('')
  const [authWho, setAuthWho] = useState('檢查 B 站登入…')
  const [authOk, setAuthOk] = useState(false)
  const [loginOpen, setLoginOpen] = useState(false)
  const [loginQr, setLoginQr] = useState('')
  const [loginStat, setLoginStat] = useState('等待掃碼…')

  const openedOnBoot = useRef(false)
  const lineId = useRef(0)
  const toastTimer = useRef<number | undefined>(undefined)
  const cancelJob = useRef<null | (() => void)>(null)

  // ---- 小工具 ----------------------------------------------------

  const toast = useCallback((msg: string, ms = 2200) => {
    setToastMsg(msg)
    window.clearTimeout(toastTimer.current)
    toastTimer.current = window.setTimeout(() => setToastMsg(''), ms)
  }, [])

  const log = useCallback((text: string, isError = false) => {
    lineId.current += 1
    const entry = { id: lineId.current, text, isError }
    setLines((prev) => {
      const next = [...prev, entry]
      return next.length > 2000 ? next.slice(-2000) : next
    })
  }, [])

  // ---- 主題 ------------------------------------------------------

  useEffect(() => { applyThemeToDocument(theme) }, [theme])

  // ---- 啟動 ------------------------------------------------------

  useEffect(() => {
    api.version().then(setBuild).catch(() => setBuild(null))
    refreshAuth()
    // 開機時自動開第一個專案（舊版就是這樣，不然一進來是空畫面）
    void loadLibrary().then((list) => {
      if (list.length && !openedOnBoot.current) {
        openedOnBoot.current = true
        void openProject(list[0]!.name)
      }
    })
  }, [])

  // ---- Esc 關閉 --------------------------------------------------

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (lightbox) { setLightbox(null); return }
      if (playing) { setPlaying(null); return }
      if (loginOpen) { setLoginOpen(false); return }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [lightbox, playing, loginOpen])

  // ---- 播放器：點外面就關 ------------------------------------------

  useEffect(() => {
    if (!playing) return
    const onDown = (e: MouseEvent) => {
      const target = e.target as HTMLElement
      if (target.closest('#player') || target.closest('#lightbox')) return
      const sel = window.getSelection()
      if (sel && sel.toString().length > 0) return
      setPlaying(null)
    }
    document.addEventListener('click', onDown)
    return () => document.removeEventListener('click', onDown)
  }, [playing])

  // ---- 資料 ------------------------------------------------------

  const loadLibrary = useCallback(async (): Promise<LibraryProject[]> => {
    try {
      const data = await api.projects()
      const list = Array.isArray(data) ? data : []
      setLibrary(list)
      return list
    } catch {
      setLibrary([])
      return []
    }
  }, [])

  const refreshAuth = useCallback(async () => {
    try {
      const st = await api.loginStatus()
      const name = String(st.nickname ?? st.name ?? '')
      const ok = Boolean(st.logged_in ?? st.ok)
      setAuthOk(ok)
      setAuthWho(ok ? `B 站：${name || '已登入'}` : 'B 站：未登入')
    } catch {
      setAuthOk(false)
      setAuthWho('B 站：無法確認')
    }
  }, [])

  const openProject = useCallback(async (name: string) => {
    try {
      const data = await api.project(name)
      setProject(data)
      setSelected(new Set())
      setPlaying(null)
    } catch (e) {
      toast(e instanceof Error ? e.message : '開不起來', 4000)
    }
  }, [toast])

  // check_ui.py 需要能主動切主題／印 log 才能量對比
  useEffect(() => {
    installDevBridge({
      setTheme,
      log,
      openLibrary: () => { setLibOpen(true); void loadLibrary() },
      closeLibrary: () => setLibOpen(false),
      closePlayer: () => setPlaying(null),
      openProject: (name: string) => { void openProject(name) },
      playClip: (rel: string, name: string) => setPlaying({ rel, name }),
    })
  }, [log, loadLibrary, openProject])

  // ---- job 串流 --------------------------------------------------

  const runJob = useCallback((jobId: string, onDone: (payload: unknown) => void) => {
    cancelJob.current?.()
    let stamp = 0
    const cancel = followJob(jobId, (ev: JobEvent) => {
      if (ev.kind === 'log') {
        log(ev.text ?? '')
        return
      }
      if (ev.kind === 'error') {
        log(ev.text ?? '串流中斷', true)
        return
      }
      // done
      const payload = (ev.payload ?? {}) as Record<string, unknown>
      if (payload.kind === 'download') {
        const pct = Number(payload.percent ?? 0)
        stamp = Number(payload.percent ?? 0)
        setDownload({
          on: true,
          done: false,
          title: String(payload.title ?? ''),
          percent: pct,
          totalPercent: Number(payload.total ?? 0),
          speed: String(payload.speed ?? ''),
          eta: payload.eta ? `剩 ${payload.eta}` : '',
          retry: Number(payload.retry ?? 0),
          maxRetries: Number(payload.max_retries ?? 10),
        })
        return
      }
      if (payload.ok === false) {
        const err = payload.error as { message?: string } | undefined
        log(err?.message ?? '失敗', true)
        toast(err?.message ?? '失敗', 4000)
        return
      }
      onDone(payload)
    })
    cancelJob.current = cancel
    void stamp
  }, [log, toast])

  // ---- 動作 ------------------------------------------------------

  const doSearch = useCallback(async () => {
    if (busy) return
    const keywords = params.keywords.split(/[\n,，]+/).map((s) => s.trim()).filter(Boolean)
    if (!keywords.length) { toast('先輸入關鍵字'); return }
    setBusy('搜尋中')
    log(`▶ 搜尋：${keywords.join('、')}`)
    try {
      const body = {
        keywords,
        platform: params.platform,
        sort: params.sort,
        per_keyword: params.per_keyword,
        min_duration: params.min_duration,
        max_duration: params.max_duration,
        fresh_days: params.fresh_days,
        min_views: params.min_views,
        exclude: params.exclude.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
        only_free: params.only_free,
        drop_reposts: params.drop_reposts,
        verify_license: params.verify_license,
      }
      const { job } = await api.search(body)
      await new Promise<void>((resolve) => {
        runJob(job, (payload) => {
          const p = payload as { count?: number; project?: string }
          toast(`找到 ${String(p.count ?? 0)} 支`)
          void loadLibrary()
          void openProject(String(p.project ?? ''))
          resolve()
        })
        window.setTimeout(resolve, 1000 * 60 * 30)
      })
    } catch (e) {
      toast(e instanceof Error ? e.message : '搜尋失敗', 4000)
    } finally {
      setBusy('')
    }
  }, [busy, params, log, toast, runJob, loadLibrary, openProject])

  const doGrab = useCallback(async () => {
    if (busy || !project) return
    if (!selected.size) { toast('先勾選要的影片'); return }

    const starts = grab.starts.split(/[,，]/).map((s) => s.trim()).filter(Boolean)
      .map(tsToSec).filter((n) => !Number.isNaN(n))
    const ranges = grab.ranges.split(/[,，]/).map((s) => s.trim()).filter(Boolean)
      .map((spec) => {
        const m = spec.split(/[-~]/)
        return m.length === 2 ? [tsToSec(m[0]!), tsToSec(m[1]!)] : null
      })
      .filter((x): x is number[] => x !== null)

    if (grab.mode === 'range' && !ranges.length) { toast('請填區段，例如 1:00-2:30'); return }

    const body = {
      project: project.name,
      indexes: [...selected],
      per: grab.per,
      starts: grab.mode === 'auto' ? starts : [],
      ranges: grab.mode === 'range' ? ranges : [],
      full: grab.mode === 'full',
      height: grab.height,
      frames: grab.frames,
      audio: grab.audio,
      save_settings: grab.saveSettings,
    }
    setBusy('下載中')
    log(grab.mode === 'full'
      ? `▶ 整支下載 ${body.indexes.length} 支（${body.height}p）`
      : `▶ 下載 ${body.indexes.length} 支，${grab.mode === 'range' ? `${ranges.length} 個區段` : `每段 ${body.per} 秒`}`)

    try {
      const { job } = await api.pick(body)
      await new Promise<void>((resolve) => {
        runJob(job, (payload) => {
          const p = payload as { grabbed?: number }
          toast(`完成 ${String(p.grabbed ?? 0)} 段`)
          setDownload((d) => ({ ...d, done: true }))
          void loadLibrary()
          void openProject(project.name)
          resolve()
        })
        window.setTimeout(resolve, 1000 * 60 * 30)
      })
    } catch (e) {
      toast(e instanceof Error ? e.message : '下載失敗', 4000)
    } finally {
      setBusy('')
      window.setTimeout(() => setDownload(NO_DOWNLOAD), 2500)
    }
  }, [busy, project, selected, grab, log, toast, runJob, loadLibrary, openProject])

  const doRefresh = useCallback(async () => {
    if (busy || !project) return
    setBusy('確認中')
    try {
      const { job } = await api.refresh({ project: project.name })
      await new Promise<void>((resolve) => {
        runJob(job, () => {
          toast('授權已更新')
          void openProject(project.name)
          resolve()
        })
        window.setTimeout(resolve, 1000 * 60 * 30)
      })
    } catch (e) {
      toast(e instanceof Error ? e.message : '更新失敗', 4000)
    } finally {
      setBusy('')
    }
  }, [busy, project, runJob, toast, openProject])

  const doOpenList = useCallback(() => {
    if (!project) return
    api.openProject({ project: project.name }).catch(() => {
      toast('開不起來', 3000)
    })
  }, [project, toast])

  const doPin = useCallback(async (name: string) => {
    const cur = library.find((p) => p.name === name)
    try {
      await api.library({ action: 'pin', project: name, pinned: !cur?.pinned })
      await loadLibrary()
    } catch (e) {
      toast(e instanceof Error ? e.message : '釘選失敗', 3000)
    }
  }, [library, loadLibrary, toast])

  const doRename = useCallback(async (from: string, to: string) => {
    try {
      await api.library({ action: 'rename', project: from, title: to })
      await loadLibrary()
      // rename 只改顯示標題，資料夾名（name）不變 —— 重開要用 from，
      // 用 to 會去要一個不存在的專案（404）。
      if (project?.name === from) await openProject(from)
      toast('已改名')
    } catch (e) {
      toast(e instanceof Error ? e.message : '改名失敗', 3000)
    }
  }, [loadLibrary, openProject, project, toast])

  const doSort = useCallback(async (mode: string) => {
    setLibSort(mode)
    try {
      await api.library({ action: 'sort', mode })
      await loadLibrary()
      const label = SORT_LABEL[mode] ?? mode
      toast(mode === 'manual' ? '已切回手動順序' : `已依「${label}」排序`)
    } catch (e) {
      toast(e instanceof Error ? e.message : '排序失敗', 3000)
    }
  }, [loadLibrary, toast])

  const doReorder = useCallback(async (order: string[]) => {
    // 手動拖曳 = 使用者自己決定順序，排序模式要跟著回到 manual
    setLibSort('manual')
    setLibrary((prev) => {
      const byName = new Map(prev.map((p) => [p.name, p]))
      return order.map((n) => byName.get(n)).filter((p): p is LibraryProject => Boolean(p))
    })
    try {
      await api.library({ action: 'order', order })
    } catch { /* 失敗就算了，下次刷新會回來 */ }
  }, [])

  const doScan = useCallback(async () => {
    setScanning(true)
    try {
      const r = await api.scan()
      setScan(r)
      await loadLibrary()
    } catch (e) {
      toast(e instanceof Error ? e.message : '掃描失敗', 3000)
    } finally {
      setScanning(false)
    }
  }, [loadLibrary, toast])

  const doAdopt = useCallback(async (folders: string[]) => {
    try {
      await api.library({ action: 'adopt', folders })
      toast(`已納入 ${folders.length} 個專案`)
      await loadLibrary()
      await doScan()
    } catch (e) {
      toast(e instanceof Error ? e.message : '納入失敗', 3000)
    }
  }, [loadLibrary, doScan, toast])

  const doForget = useCallback(async (folders: string[]) => {
    try {
      await api.library({ action: 'forget', folders })
      toast('已移除空記錄')
      await loadLibrary()
      await doScan()
    } catch (e) {
      toast(e instanceof Error ? e.message : '移除失敗', 3000)
    }
  }, [loadLibrary, doScan, toast])

  const doLogin = useCallback(async () => {
    try {
      const { key, qr } = await api.loginStart()
      setLoginQr(qr)
      setLoginStat('等待掃碼…')
      setLoginOpen(true)
      const timer = window.setInterval(async () => {
        try {
          const st = await api.loginPoll(key)
          if (st.ok === true) {
            window.clearInterval(timer)
            setLoginOpen(false)
            setLoginStat('登入成功')
            toast('登入成功')
            await refreshAuth()
          } else if (st.status) {
            setLoginStat(String(st.status))
          }
        } catch { window.clearInterval(timer) }
      }, 2000)
    } catch (e) {
      toast(e instanceof Error ? e.message : '登入啟動失敗', 3000)
    }
  }, [refreshAuth, toast])

  const doOpenAdmin = useCallback(async () => {
    try {
      const data = await api.config() as unknown as { path: string; fields?: ConfigField[] }
      if (data.fields) setConfig({ path: data.path, fields: data.fields })
      setAdminOpen(true)
    } catch (e) {
      toast(e instanceof Error ? e.message : '讀不到設定', 3000)
    }
  }, [toast])

  const doSaveConfig = useCallback(async (patch: Record<string, unknown>) => {
    try {
      await api.setConfig(patch)
      toast('設定已儲存')
      const fresh = await api.config() as unknown as {
        path: string; fields?: ConfigField[]; values?: Record<string, unknown>
      }
      if (fresh.fields) setConfig({ path: fresh.path, fields: fresh.fields })
      await refreshAuth()
    } catch (e) {
      toast(e instanceof Error ? e.message : '儲存失敗', 4000)
    }
  }, [toast, refreshAuth])

  const doResetConfig = useCallback(async (keys: string[] | null) => {
    try {
      await api.resetConfig(keys)
      toast('已還原預設')
      const fresh = await api.config() as unknown as {
        path: string; fields?: ConfigField[]
      }
      if (fresh.fields) setConfig({ path: fresh.path, fields: fresh.fields })
    } catch (e) {
      toast(e instanceof Error ? e.message : '還原失敗', 3000)
    }
  }, [toast])

  const toggle = useCallback((index: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })
  }, [])

  useSidebarResizer(true)

  const activeTheme = findTheme(theme)

  return (
    <>
      <div className="app">
        <Sidebar
          params={params}
          onChange={(patch) => setParams((p) => ({ ...p, ...patch }))}
          onSearch={doSearch}
          busy={Boolean(busy)}
          authWho={authWho}
          authOk={authOk}
          onLogin={doLogin}
          build={build ? `build ${build.build}` : ''}
          themeNode={<ThemePicker theme={theme} onPick={setTheme} />}
          onOpenLibrary={() => setLibOpen(true)}
          onOpenAdmin={() => { void doOpenAdmin() }}
          projectsNode={(
            <>
              {library.slice(0, 8).map((p) => (
                <div key={p.name}
                     className={`projrow${project?.name === p.name ? ' active' : ''}`}
                     onClick={() => { void openProject(p.name); setLibOpen(false) }}>
                  {p.pinned ? '📌 ' : ''}{p.title || p.name}
                  <span className="n">{p.count}</span>
                </div>
              ))}
              {library.length === 0
                ? <div className="hint" style={{ padding: '4px 2px' }}>還沒有專案</div>
                : null}
            </>
          )}
        />

        <main>
          <Player playing={playing} onClose={() => setPlaying(null)} />

          <div id="view">
            {project
              ? (
                <ProjectView
                  project={project}
                  selected={selected}
                  options={grab}
                  onOptions={(patch) => setGrab((g) => ({ ...g, ...patch }))}
                  onToggle={toggle}
                  onSelectAll={(on) => setSelected(
                    on ? new Set(project.items.map((i) => i.index)) : new Set(),
                  )}
                  onPlay={(rel, name) => setPlaying({ rel, name })}
                  onLightbox={(kind, src) => setLightbox({ kind, src })}
                  onGrab={() => { void doGrab() }}
                  onRefresh={() => { void doRefresh() }}
                  onOpenList={doOpenList}
                  busy={Boolean(busy)}
                />
              )
              : (
                <div className="empty">
                  <div className="big">🎬</div>
                  先在左邊打關鍵字，按「搜尋素材」。<br />
                  搜完可以勾選想要的，只下載你要的那幾秒。
                </div>
              )}
          </div>
        </main>
      </div>

      <ProgressBar percent={download.on ? download.totalPercent : null} />
      <DownloadCard
        visible={download.on}
        title={download.title}
        percent={download.percent}
        totalPercent={download.totalPercent}
        speed={download.speed}
        eta={download.eta}
        done={download.done}
        retry={download.retry}
        maxRetries={download.maxRetries}
      />
      <Console lines={lines} />
      <Toast message={toastMsg} />
      <LoginModal open={loginOpen} qr={loginQr} status={loginStat}
                  onClose={() => setLoginOpen(false)} />
      <LibraryPanel
        open={libOpen}
        projects={library}
        currentName={project?.name ?? null}
        sort={libSort}
        onSort={(v) => { void doSort(v) }}
        onClose={() => setLibOpen(false)}
        onPick={(n) => { void openProject(n); setLibOpen(false) }}
        onRename={(f, t) => { void doRename(f, t) }}
        onPin={(n) => { void doPin(n) }}
        scan={scan}
        scanning={scanning}
        onScan={() => { void doScan() }}
        onReorder={(o) => { void doReorder(o) }}
        onAdopt={(f) => { void doAdopt(f) }}
        onForget={(f) => { void doForget(f) }}
      />

      <AdminPanel
        open={adminOpen}
        config={config}
        onClose={() => setAdminOpen(false)}
        onSave={(p) => { void doSaveConfig(p) }}
        onReset={(k) => { void doResetConfig(k) }}
      />

      <div className={`lightbox${lightbox ? ' show' : ''}`} id="lightbox"
           onClick={() => setLightbox(null)}>
        {lightbox ? (
          lightbox.kind === 'video' ? (
            <video src={lightbox.src} controls autoPlay playsInline muted
                   onClick={(e) => e.stopPropagation()} />
          ) : (
            <img src={lightbox.src} alt="放大檢視" />
          )
        ) : null}
      </div>

      <div className="sr-only" aria-live="polite">
        {busy ? `${busy}中` : ''}
        {activeTheme ? '' : ''}
      </div>
    </>
  )
}
