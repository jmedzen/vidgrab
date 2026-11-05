/**
 * 給 tools/check_ui.py 用的除錯介面。
 *
 * check_ui.py 是行為級測試：它要能真的切主題、真的往 log 印一行，
 * 才有辦法量到「淺色主題下 log 的字跟底對比夠不夠」。這些操作在
 * React 裡是內部狀態，所以開一組明確的窗口出來 —— 只做測試需要的事，
 * 不把整個 store 攤開。
 */
import {
  THEMES, applyThemeToDocument, clampSidebar, SIDEBAR_KEY,
} from './theme'
import { rawApi } from './api'

interface DevBridge {
  THEMES: typeof THEMES
  api: (path: string, opts?: RequestInit) => Promise<unknown>
  applyTheme: (id: string) => void
  consoleLine: (text: string, isErr?: boolean) => void
  openLibrary: () => void
  closeLibrary: () => void
  closePlayer: () => void
  openProject: (name: string) => void
  playClip: (rel: string, name: string) => void
}

declare global {
  interface Window {
    __vidgrab?: DevBridge
  }
}

export function installDevBridge(opts: {
  setTheme: (id: string) => void
  log: (text: string, isErr?: boolean) => void
  openLibrary: () => void
  closeLibrary: () => void
  closePlayer: () => void
  openProject: (name: string) => void
  playClip: (rel: string, name: string) => void
}): void {
  if (typeof window === 'undefined') return
  window.__vidgrab = {
    THEMES,
    applyTheme: (id: string) => {
      applyThemeToDocument(id)
      opts.setTheme(id)
    },
    consoleLine: opts.log,
    api: rawApi,
    openLibrary: opts.openLibrary,
    closeLibrary: opts.closeLibrary,
    closePlayer: opts.closePlayer,
    openProject: opts.openProject,
    playClip: opts.playClip,
  }
  // check_ui.py 直接呼叫這些名字，沿用舊版的寫法
  const bridge = window.__vidgrab
  Object.assign(window, {
    THEMES,
    api: rawApi,
    applyTheme: bridge.applyTheme,
    consoleLine: opts.log,
    // 舊版有個 $ 是 querySelector 的縮寫，測試腳本用它點按鈕。
    // 少了它，那些 .click() 會靜默失敗 —— 看起來像選單打不開。
    $: (sel: string) => document.querySelector(sel),
    openLibrary: opts.openLibrary,
    closeLibrary: opts.closeLibrary,
    closePlayer: opts.closePlayer,
    openProject: opts.openProject,
    playClip: opts.playClip,
    setSidebar: (px: number, persist: boolean) => {
      const width = clampSidebar(px)
      document.documentElement.style.setProperty('--sidebar', `${width}px`)
      if (persist) {
        try { localStorage.setItem(SIDEBAR_KEY, String(width)) } catch { /* 忽略 */ }
      }
      return width
    },
  })
}
