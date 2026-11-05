import { useEffect, useRef } from 'react'

export interface LogLine {
  id: number
  text: string
  isError: boolean
}

interface Props {
  lines: LogLine[]
}

export default function Console({ lines }: Props) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = ref.current
    if (el) el.scrollTop = el.scrollHeight
  }, [lines])

  return (
    <div id="console" ref={ref} role="log" aria-live="polite" aria-label="執行日誌">
      {lines.map((l) => (
        <div className={`ln${l.isError ? ' err' : ''}`} key={l.id}>{l.text}</div>
      ))}
    </div>
  )
}
