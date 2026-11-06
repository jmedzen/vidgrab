import { useEffect, useRef } from 'react'

export interface LogLine {
  id: number
  text: string
  isError: boolean
}

interface Props {
  lines: LogLine[]
  /** 收合狀態由外層管：重新整理後要記得使用者收起來過。 */
  open: boolean
  onToggle: () => void
}

export default function Console({ lines, open, onToggle }: Props) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    document.body.classList.toggle('log-closed', !open)
  }, [open])

  useEffect(() => {
    const el = ref.current
    if (el) el.scrollTop = el.scrollHeight
  }, [lines, open])

  const last = lines.length ? lines[lines.length - 1] : null

  return (
    <div id="logdock" className={open ? '' : 'closed'}>
      <div id="consolebar" onClick={onToggle}>
        <span className="cb-title">
          {last ? last.text.replace(/^\s+/, '').slice(0, 90) : '執行日誌'}
        </span>
        <span>{lines.length} 行</span>
        <button
          className="cb-toggle"
          id="logtoggle"
          aria-expanded={open}
          aria-controls="console"
          aria-label={open ? '收合執行日誌' : '展開執行日誌'}
          onClick={(e) => { e.stopPropagation(); onToggle() }}
        >
          {open ? '收合 ▾' : '展開 ▴'}
        </button>
      </div>
      <div id="console" ref={ref} role="log" aria-live="polite" aria-label="執行日誌">
        {lines.length === 0
          ? <div className="ln empty">開始之後，抓取與下載的進度會顯示在這裡。</div>
          : lines.map((l) => (
            <div className={`ln${l.isError ? ' err' : ''}`} key={l.id}>{l.text}</div>
          ))}
      </div>
    </div>
  )
}
