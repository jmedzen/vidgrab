import { useEffect } from 'react'
import { SIDEBAR_DEFAULT, SIDEBAR_KEY, SIDEBAR_MAX, SIDEBAR_MIN, clampSidebar } from './theme'

function readVar(): number {
  const raw = getComputedStyle(document.documentElement).getPropertyValue('--sidebar')
  const n = Number.parseInt(raw, 10)
  return Number.isFinite(n) && n > 0 ? n : SIDEBAR_DEFAULT
}

function setSidebar(px: number, persist: boolean): number {
  const width = clampSidebar(px)
  document.documentElement.style.setProperty('--sidebar', `${width}px`)
  if (persist) {
    try { localStorage.setItem(SIDEBAR_KEY, String(width)) } catch { /* 忽略 */ }
  }
  return width
}

/** 側欄寬度：拖曳、雙擊還原、鍵盤調整、視窗變窄時自動收斂。 */
export function useSidebarResizer(active: boolean): void {
  useEffect(() => {
    const bar = document.getElementById('resizer')
    if (!bar) return

    let saved: number | null = null
    try {
      const raw = localStorage.getItem(SIDEBAR_KEY)
      if (raw) saved = Number(raw)
    } catch { /* 忽略 */ }

    const maxForViewport = Math.min(SIDEBAR_MAX, Math.round(window.innerWidth * 0.55))
    const initial = saved && saved >= SIDEBAR_MIN && saved <= SIDEBAR_MAX
      ? saved
      : Math.min(SIDEBAR_DEFAULT, maxForViewport)
    setSidebar(initial, false)

    let dragging = false

    const onMove = (e: MouseEvent) => {
      if (!dragging) return
      setSidebar(e.clientX, false)
    }

    const stop = () => {
      if (!dragging) return
      dragging = false
      bar.classList.remove('dragging')
      document.body.classList.remove('resizing')
      setSidebar(readVar(), true)
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', stop)
    }

    const onDown = (e: MouseEvent) => {
      e.preventDefault()
      dragging = true
      bar.classList.add('dragging')
      document.body.classList.add('resizing')
      document.addEventListener('mousemove', onMove)
      document.addEventListener('mouseup', stop)
    }

    const onDblClick = () => { setSidebar(SIDEBAR_DEFAULT, true) }

    const onKey = (e: KeyboardEvent) => {
      const cur = readVar()
      if (e.key === 'ArrowLeft') { e.preventDefault(); setSidebar(cur - 16, true) }
      if (e.key === 'ArrowRight') { e.preventDefault(); setSidebar(cur + 16, true) }
      if (e.key === 'Home') { e.preventDefault(); setSidebar(SIDEBAR_DEFAULT, true) }
    }

    const onResize = () => {
      if (window.innerWidth <= 820) return
      setSidebar(Math.min(readVar(), Math.round(window.innerWidth * 0.55)), false)
    }

    bar.addEventListener('mousedown', onDown)
    bar.addEventListener('dblclick', onDblClick)
    bar.addEventListener('keydown', onKey as EventListener)
    window.addEventListener('resize', onResize)

    return () => {
      bar.removeEventListener('mousedown', onDown)
      bar.removeEventListener('dblclick', onDblClick)
      bar.removeEventListener('keydown', onKey as EventListener)
      window.removeEventListener('resize', onResize)
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', stop)
    }
  }, [active])
}
