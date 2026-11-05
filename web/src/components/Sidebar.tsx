import type { ReactNode } from 'react'

export interface SearchParams {
  keywords: string
  platform: string
  sort: string
  per_keyword: number
  min_duration: number
  max_duration: number
  fresh_days: number
  min_views: number
  exclude: string
  only_free: boolean
  drop_reposts: boolean
  verify_license: boolean
}

export const DEFAULT_PARAMS: SearchParams = {
  keywords: '',
  platform: 'both',
  sort: 'smart',
  per_keyword: 12,
  min_duration: 10,
  max_duration: 0,
  fresh_days: 0,
  min_views: 0,
  exclude: '',
  only_free: false,
  drop_reposts: false,
  verify_license: false,
}

interface FieldProps {
  label: string
  children: ReactNode
}

function Field({ label, children }: FieldProps) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  )
}

interface Props {
  params: SearchParams
  onChange: (patch: Partial<SearchParams>) => void
  onSearch: () => void
  busy: boolean
  authWho: string
  authOk: boolean
  onLogin: () => void
  build: string
  projectsNode: ReactNode
  themeNode: ReactNode
  onOpenLibrary: () => void
  onOpenAdmin: () => void
}

export default function Sidebar({
  params, onChange, onSearch, busy, authWho, authOk, onLogin, build,
  projectsNode, themeNode, onOpenLibrary, onOpenAdmin,
}: Props) {

  return (
    <aside>
      <div className="brandrow">
        <div>
          <h1>vid<span>grab</span></h1>
          <div className="tagline">打關鍵字 → 挑片 → 只切你要的那幾秒</div>
        </div>
        {themeNode}
      </div>
      <div className="buildtag" id="buildtag">
        {build}
        <button className="mini" id="openadmin" title="系統設定"
                style={{ marginLeft: 8, minWidth: 0, minHeight: 28, padding: '2px 9px' }}
                onClick={onOpenAdmin}>
          ⚙ 設定
        </button>
      </div>

      <Field label="關鍵字（一行一個，或用逗號分隔）">
        <textarea
          id="keywords"
          placeholder={'台北 街景\n北歐 空景\n寺廟 特寫'}
          value={params.keywords}
          onChange={(e) => onChange({ keywords: e.target.value })}
        />
      </Field>

      <div className="row">
        <Field label="平台">
          <select id="platform" value={params.platform}
                  onChange={(e) => onChange({ platform: e.target.value })}>
            <option value="both">B 站 + YouTube</option>
            <option value="bilibili">只要 B 站</option>
            <option value="youtube">只要 YouTube</option>
          </select>
        </Field>
        <Field label="排序">
          <select id="sort" value={params.sort}
                  onChange={(e) => onChange({ sort: e.target.value })}>
            <option value="smart">智慧（授權＋熱度）</option>
            <option value="views">觀看最多</option>
            <option value="new">最新</option>
            <option value="license">授權優先</option>
          </select>
        </Field>
      </div>

      <div className="row">
        <Field label="每字幾支">
          <input type="number" id="per_keyword" value={params.per_keyword} min={1} max={50}
                 onChange={(e) => onChange({ per_keyword: Number(e.target.value) || 12 })} />
        </Field>
        <Field label="最短（分鐘）">
          <input type="number" id="min_duration" value={params.min_duration} min={0} step={1}
                 onChange={(e) => onChange({ min_duration: Number(e.target.value) || 0 })} />
        </Field>
        <Field label="最長（0=不限）">
          <input type="number" id="max_duration" value={params.max_duration} min={0} step={1}
                 onChange={(e) => onChange({ max_duration: Number(e.target.value) || 0 })} />
        </Field>
      </div>

      <div className="row">
        <Field label="最近 N 天（0=不限）">
          <input type="number" id="fresh_days" value={params.fresh_days} min={0}
                 onChange={(e) => onChange({ fresh_days: Number(e.target.value) || 0 })} />
        </Field>
        <Field label="最少觀看">
          <input type="number" id="min_views" value={params.min_views} min={0} step={100}
                 onChange={(e) => onChange({ min_views: Number(e.target.value) || 0 })} />
        </Field>
      </div>

      <Field label="排除詞（逗號分隔，標題含這些就跳過）">
        <input type="text" id="exclude" placeholder="ex. 合集,直播回放"
               value={params.exclude}
               onChange={(e) => onChange({ exclude: e.target.value })} />
      </Field>

      <div className="checks">
        <label>
          <input type="checkbox" id="only_free" checked={params.only_free}
                 onChange={(e) => onChange({ only_free: e.target.checked })} /> 只留授權明確的
        </label>
        <label>
          <input type="checkbox" id="drop_reposts" checked={params.drop_reposts}
                 onChange={(e) => onChange({ drop_reposts: e.target.checked })} /> 丟掉轉載／非原始出處
        </label>
        <label>
          <input type="checkbox" id="verify_license" checked={params.verify_license}
                 onChange={(e) => onChange({ verify_license: e.target.checked })} /> 逐支確認授權（慢，較準）
        </label>
      </div>

      <div id="authbox" className="authbox">
        <span id="authwho">{authWho}</span>
        <button className="tiny ghost" id="loginbtn" style={{ whiteSpace: 'nowrap', flex: 'none' }}
                onClick={onLogin}>
          {authOk ? '重新登入' : '掃碼登入'}
        </button>
      </div>

      <button className="primary" id="go" disabled={busy} onClick={onSearch}>
        {busy ? '處理中…' : '搜尋素材'}
      </button>
      <div className="hint" style={{ marginTop: 8 }}>
        長度預設 10 分鐘以上，適合找可反覆取用的長素材。
      </div>

      <div className="proj-list">
        <h3>
          我的素材專案
          <button className="mini" id="openlib" title="展開素材庫"
                  onClick={onOpenLibrary}>⤢ 展開</button>
        </h3>
        <div id="projects">{projectsNode}</div>
      </div>

      <div className="resizer" id="resizer" role="separator"
           aria-orientation="vertical" tabIndex={0}
           title="拖曳調整寬度（雙擊還原）" />
    </aside>
  )
}
