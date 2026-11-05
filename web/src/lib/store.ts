import { useSyncExternalStore } from 'react'
import type { LibraryProject, Project } from '../types'

/**
 * 全域狀態。
 *
 * 舊版是 9 個散落的 `let`（CURRENT / SELECTED / PLAYING / LIB…），
 * 搭配手動呼叫 render() 重繪 —— 這種寫法最常出錯的地方是
 * 「資料變了，但忘記叫重繪」或「重繪了，但狀態早就過期」。
 * 這裡全部收成單一 store，React 訂閱它就會自動重繪。
 */
export interface State {
  current: Project | null
  selected: ReadonlySet<number>
  playing: string | null
  library: LibraryProject[]
  libOpen: boolean
  busy: boolean
  busyLabel: string
  theme: string
  sidebar: number
}

const initial: State = {
  current: null,
  selected: new Set<number>(),
  playing: null,
  library: [],
  libOpen: false,
  busy: false,
  busyLabel: '',
  theme: 'midnight',
  sidebar: 330,
}

let state: State = initial
const listeners = new Set<() => void>()

function emit(): void {
  for (const fn of listeners) fn()
}

export function getState(): State {
  return state
}

export function setState(patch: Partial<State> | ((s: State) => Partial<State>)): void {
  const next = typeof patch === 'function' ? patch(state) : patch
  state = { ...state, ...next }
  emit()
}

export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

/** 元件用這個拿狀態；只有被用到的欄位變動才會重繪。 */
export function useStore<T>(pick: (s: State) => T): T {
  return useSyncExternalStore(
    subscribe,
    () => pick(state),
    () => pick(initial),
  )
}

// ---- 勾選（Set 是不可變的，改動一律換新的） --------------------------

export function toggleSelected(index: number): void {
  const next = new Set(state.selected)
  if (next.has(index)) next.delete(index)
  else next.add(index)
  setState({ selected: next })
}

export function selectAll(indexes: readonly number[], on: boolean): void {
  setState({ selected: on ? new Set(indexes) : new Set<number>() })
}
