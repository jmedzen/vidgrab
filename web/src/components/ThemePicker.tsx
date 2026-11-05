import { useEffect, useRef, useState } from 'react'
import { THEMES, kindIcon } from '../lib/theme'

interface Props {
  theme: string
  onPick: (id: string) => void
}

export default function ThemePicker({ theme, onPick }: Props) {
  const [open, setOpen] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const active = THEMES.find((t) => t.id === theme) ?? THEMES[0]!

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!boxRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="theme-picker" ref={boxRef}>
      <button className="theme-btn" id="themebtn" title="切換主題"
              aria-haspopup="true" aria-expanded={open}
              onClick={() => setOpen(true)}>
        <span id="themeicon">{kindIcon(active.kind)}</span>
      </button>
      <div className={`theme-menu${open ? ' open' : ''}`} id="thememenu" role="menu">
          {(['dark', 'light'] as const).map((kind) => (
            <div key={kind}>
              <div className="group">{kind === 'dark' ? '深色' : '淺色'}</div>
              {THEMES.filter((t) => t.kind === kind).map((t) => (
                <button key={t.id} role="menuitemradio"
                        aria-checked={t.id === theme}
                        data-theme={t.id}
                        onClick={() => { onPick(t.id); setOpen(false) }}>
                  <span className="swatch" style={{ background: t.dot, boxShadow: `inset 0 0 0 2px ${t.accent}` }} />
                  <span>{t.name}</span>
                </button>
              ))}
            </div>
          ))}
        </div>
    </div>
  )
}
