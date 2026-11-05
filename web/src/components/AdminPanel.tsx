import { useEffect, useState } from 'react'

export interface ConfigField {
  key: string
  group: string
  label: string
  type: string
  value: unknown
  default: unknown
  help: string
  changed: boolean
  options?: string[]
  min?: number
  max?: number
}

export interface ConfigState {
  path: string
  fields: ConfigField[]
}

interface Props {
  open: boolean
  config: ConfigState | null
  onClose: () => void
  onSave: (patch: Record<string, unknown>) => void
  onReset: (keys: string[] | null) => void
}

const GROUP_ORDER = ['搜尋', '授權', '下載', '服務']

function labelOf(f: ConfigField, v: unknown): string {
  if (f.key === 'platform') {
    return ({ both: 'B 站 + YouTube', bilibili: '只要 B 站', youtube: '只要 YouTube' } as Record<string, string>)[String(v)] ?? String(v)
  }
  if (f.key === 'sort') {
    return ({ smart: '智慧（授權＋熱度）', views: '觀看最多', new: '最新', license: '授權優先' } as Record<string, string>)[String(v)] ?? String(v)
  }
  if (f.key === 'order') {
    return ({ smart: '智慧', relevance: '相關度', views: '觀看', new: '最新' } as Record<string, string>)[String(v)] ?? String(v)
  }
  return String(v)
}

export default function AdminPanel({ open, config, onClose, onSave, onReset }: Props) {
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [dirty, setDirty] = useState(false)

  useEffect(() => {
    if (!open || !config) return
    const next: Record<string, unknown> = {}
    for (const f of config.fields) next[f.key] = f.value
    setDraft(next)
    setDirty(false)
  }, [open, config])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  const set = (key: string, value: unknown) => {
    setDraft((d) => ({ ...d, [key]: value }))
    setDirty(true)
  }

  const save = () => {
    const patch: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(draft)) patch[k] = v
    onSave(patch)
    setDirty(false)
  }

  const groups = GROUP_ORDER.filter((g) => config?.fields.some((f) => f.group === g))

  return (
    <div className={`libmodal${open ? ' show' : ''}`} id="adminmodal"
         role="dialog" aria-modal="true" aria-labelledby="adminTitle"
         onClick={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="libpanel" style={{ width: 'min(920px,100%)' }}>
        <div className="libhead">
          <h2 id="adminTitle">系統設定</h2>
          <span className="count">{dirty ? '有未存的變更' : ''}</span>
          <span className="grow" />
          <button className="ghost tiny" id="adminreset"
                  title="全部回到預設值" onClick={() => onReset(null)}>
            全部還原
          </button>
          <button className="ghost tiny" id="adminclose" onClick={onClose}>關閉</button>
          <button className="primary" id="adminsave" style={{ width: 'auto', padding: '6px 16px' }}
                  disabled={!dirty} onClick={save}>
            儲存
          </button>
        </div>

        <div className="libbody">
          <div className="hint" style={{ marginBottom: 12 }}>
            存檔位置：<code>{config?.path ?? ''}</code>
          </div>

          {groups.map((group) => (
            <section key={group} style={{ marginBottom: 22 }}>
              <h3 style={{ fontSize: 13, color: 'var(--muted)', letterSpacing: '.08em',
                           margin: '0 0 10px' }}>
                {group}
              </h3>
              <div style={{ display: 'grid', gap: 10 }}>
                {config?.fields.filter((f) => f.group === group).map((f) => (
                  <div key={f.key}
                       style={{ display: 'grid',
                                gridTemplateColumns: 'minmax(160px,220px) 1fr',
                                gap: 12, alignItems: 'center' }}>
                    <label htmlFor={`cfg-${f.key}`}
                           style={{ fontSize: 12.5, color: 'var(--muted)',
                                    display: 'flex', alignItems: 'center', gap: 6 }}>
                      {f.label}
                      {f.changed ? <span title="與預設值不同" style={{ color: 'var(--accent)' }}>•</span> : null}
                    </label>
                    <div>
                      <ConfigInput field={f} value={draft[f.key]}
                                   onChange={(v) => set(f.key, v)} />
                      {f.help ? (
                        <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 3 }}>
                          {f.help}
                        </div>
                      ) : null}
                    </div>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>
    </div>
  )
}

interface InputProps {
  field: ConfigField
  value: unknown
  onChange: (v: unknown) => void
}

function ConfigInput({ field, value, onChange }: InputProps) {
  const id = `cfg-${field.key}`

  if (field.type === 'bool') {
    return (
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 13 }}>
        <input id={id} type="checkbox" checked={Boolean(value)}
               onChange={(e) => onChange(e.target.checked)} />
        <span style={{ color: 'var(--muted)', fontSize: 12 }}>{value ? '開' : '關'}</span>
      </label>
    )
  }

  if (field.options) {
    return (
      <select id={id} value={String(value)} aria-label={field.label}
              onChange={(e) => onChange(e.target.value)}>
        {field.options.map((o) => <option key={o} value={o}>{labelOf(field, o)}</option>)}
      </select>
    )
  }

  if (field.type === 'list') {
    return (
      <input id={id} type="text" aria-label={field.label}
             value={Array.isArray(value) ? value.join(', ') : String(value ?? '')}
             placeholder="逗號分隔"
             onChange={(e) => onChange(
               e.target.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
             )} />
    )
  }

  if (field.type === 'int' || field.type === 'float') {
    return (
      <input id={id} type="number" aria-label={field.label}
             value={Number(value ?? 0)}
             step={field.type === 'float' ? '0.5' : '1'}
             min={field.min} max={field.max}
             onChange={(e) => onChange(
               field.type === 'float' ? Number(e.target.value) : Number.parseInt(e.target.value, 10) || 0,
             )} />
    )
  }

  return (
    <input id={id} type="text" aria-label={field.label}
           value={String(value ?? '')}
           onChange={(e) => onChange(e.target.value)} />
  )
}
