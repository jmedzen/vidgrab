import { useEffect, useRef, useState } from 'react'

interface Props {
  /** null = 沒在播；元素仍留在 DOM，只是隱藏（跟舊版一致，測試也依賴這點） */
  playing: { rel: string; name: string } | null
  onClose: () => void
}

export default function Player({ playing, onClose }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [failed, setFailed] = useState(false)
  const rel = playing?.rel ?? ''

  useEffect(() => {
    const v = videoRef.current
    if (!v) return
    setFailed(false)
    if (!rel) {
      v.pause()
      v.removeAttribute('src')
      v.load()
      return
    }
    v.load()
    v.play().catch(() => {
      /* 瀏覽器可能擋自動播放，讓使用者自己按 */
    })
  }, [rel])

  // 影片解不開時給明確的一句話，否則只看到一片黑
  useEffect(() => {
    const v = videoRef.current
    if (!v) return
    const onErr = () => setFailed(true)
    v.addEventListener('error', onErr)
    return () => v.removeEventListener('error', onErr)
  }, [])

  return (
    <div className="player" id="player" style={playing ? undefined : { display: 'none' }}>
      <div className="ptitle">
        <b id="playername">{playing?.name ?? '—'}</b>
        <span id="playermeta">
          {failed ? '這個格式瀏覽器可能不支援，請按「下載這個檔案」' : ''}
        </span>
      </div>
      <div className="stage">
        <video ref={videoRef} id="videoplayer" controls preload="metadata" playsInline
               src={rel || undefined} />
      </div>
      <div className="prow">
        <button className="mini" id="playerclose" onClick={onClose}>關閉</button>
        <a className="mini" id="playerdownload" href={rel || undefined} download>下載這個檔案</a>
        <span id="playerframes" className="frames" />
      </div>
    </div>
  )
}
