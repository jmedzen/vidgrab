import { useEffect } from 'react'

interface Props {
  visible: boolean
  title: string
  percent: number
  totalPercent: number
  speed: string
  eta: string
  done: boolean
  /** 第幾次重試（0 = 沒在重試） */
  retry: number
  maxRetries: number
}

export default function DownloadCard({
  visible, title, percent, totalPercent, speed, eta, done, retry, maxRetries,
}: Props) {
  // 下載卡出現時把 #console 往上推，否則 log 最後幾行會被蓋住
  useEffect(() => {
    document.body.classList.toggle('dl-on', visible)
    return () => document.body.classList.remove('dl-on')
  }, [visible])

  if (!visible) return null
  return (
    <div id="dlcard">
      <div className="row1">
        <span className="badge" id="dlbadge">
          {done ? '完成' : retry > 0 ? `重試 ${retry}/${maxRetries}` : '下載中'}
        </span>
        <span className="name" id="dlname">{title || '準備中…'}</span>
        <span className="pct" id="dlpct" role="status" aria-live="polite"
              aria-label="下載進度">{Math.round(percent)}%</span>
      </div>
      <div id="dlbar"><div id="dlfill" style={{ width: `${percent}%` }} /></div>
      <div id="dltotal"><div id="dltotalfill" style={{ width: `${totalPercent}%` }} /></div>
      <div id="dlmeta">
        <span id="dlspeed">{speed}</span>
        <span id="dleta">{eta}</span>
      </div>
    </div>
  )
}
