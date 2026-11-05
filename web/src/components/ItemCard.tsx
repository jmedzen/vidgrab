import type { Item } from '../types'
import { LIC_CLASS } from '../lib/format'

interface Props {
  item: Item
  selected: boolean
  onToggle: (index: number) => void
  onPlay: (rel: string, name: string) => void
  onLightbox: (src: string) => void
}

export default function ItemCard({ item, selected, onToggle, onPlay, onLightbox }: Props) {
  const licenceCls = LIC_CLASS[item.license_kind] || 'lic-unknown'
  // 「未知」幾乎每支都是，掛在每張卡片上等於雜訊 → 只有真的查出授權才顯示
  const showLicence = Boolean(item.license_kind) && item.license_kind !== 'unknown'

  const notes: string[] = []
  if (item.repost) notes.push('轉載／非原始出處')
  if (item.note) notes.push(item.note)
  if (item.error) notes.push(item.error)

  return (
    <div className={`card${selected ? ' sel' : ''}`} data-idx={item.index}>
      <input
        type="checkbox"
        data-idx={item.index}
        checked={selected}
        onChange={() => onToggle(item.index)}
        aria-label={`選擇第 ${item.index + 1} 支`}
      />
      <div>
        <h3>
          <a href={item.url} target="_blank" rel="noopener">
            {item.index + 1}. {item.title}
          </a>
        </h3>
        <div className="meta">
          <span>{item.platform_label}</span>
          <span>{item.author || '—'}</span>
          <span>時長 <b>{item.duration}</b></span>
          <span>觀看 <b>{item.views_label}</b></span>
          <span>{item.pubdate || '—'}</span>
          {showLicence
            ? <span className={`pill ${licenceCls}`}>{item.license_short}</span>
            : null}
        </div>

        {item.thumbs.length
          ? (
            <div className="thumbs">
              {item.thumbs.map((t) => (
                <img key={t} src={t} loading="lazy" alt="影片畫面"
                     onClick={() => onLightbox(t)} />
              ))}
            </div>
          )
          : null}

        {notes.length
          ? <div className="warnline">⚠ {notes.join('；')}</div>
          : null}

        {item.clips.length
          ? (
            <div className="clipbox">
              {item.clips.map((c) => (
                <div className="clip-row" key={c.rel}>
                  {c.thumb
                    ? (
                      <img className="clip-thumb" src={c.thumb} loading="lazy"
                           alt={`${c.name} 的畫面預覽`} title="這一格就是這個片段"
                           onClick={() => onLightbox(c.thumb)} />
                    )
                    : <span className="clip-thumb clip-nothumb" title="沒有縮圖">無圖</span>}
                  <a href={c.rel} data-play={c.rel} data-name={c.name}
                     onClick={(e) => { e.preventDefault(); onPlay(c.rel, c.name) }}>
                    {c.name}
                  </a>
                  <code>{c.span}</code>
                  <span>{c.dims} · {c.size}</span>
                  <span className="clip-actions">
                    <button className="mini" data-play={c.rel} data-name={c.name}
                            onClick={() => onPlay(c.rel, c.name)}>▶ 播放</button>
                    <a className="mini" href={c.rel} download>下載</a>
                  </span>
                </div>
              ))}
            </div>
          )
          : null}
      </div>
    </div>
  )
}
