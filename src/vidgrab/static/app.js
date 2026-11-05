const $ = (s) => document.querySelector(s);

// ------------------------------------------------------------ 主題與側欄寬度
const THEMES = [
  {id:'midnight', name:'Midnight', kind:'dark',  dot:'#0b0d12', accent:'#6cc0ff'},
  {id:'ink',      name:'Ink',      kind:'dark',  dot:'#08090b', accent:'#c9a227'},
  {id:'forest',   name:'Forest',   kind:'dark',  dot:'#0a1210', accent:'#6fd6a8'},
  {id:'paper',    name:'Paper',    kind:'light', dot:'#f7f4ee', accent:'#b5651d'},
  {id:'mist',     name:'Mist',     kind:'light', dot:'#eef2f7', accent:'#2f6fd0'},
  {id:'sakura',   name:'Sakura',   kind:'light', dot:'#fdf4f5', accent:'#c25a7a'},
];
const THEME_KEY = 'vidgrab.theme';
const SIDEBAR_KEY = 'vidgrab.sidebar';
const SIDEBAR_MIN = 240, SIDEBAR_MAX = 620, SIDEBAR_DEFAULT = 330;

function applyTheme(id) {
  const t = THEMES.find(x => x.id === id) || THEMES[0];
  document.documentElement.dataset.theme = t.id;
  $('#themeicon').textContent = t.kind === 'dark' ? '◐' : '◑';
  // 讓瀏覽器把捲軸、表單控件也跟著換色
  document.documentElement.style.colorScheme = t.kind;
  document.querySelectorAll('#thememenu button').forEach(b => {
    b.setAttribute('aria-checked', String(b.dataset.theme === t.id));
  });
  try { localStorage.setItem(THEME_KEY, t.id); } catch (e) {}
}

function buildThemeMenu() {
  const menu = $('#thememenu');
  const groups = [['dark', '深色'], ['light', '淺色']];
  menu.innerHTML = groups.map(([kind, label]) => `
    <div class="group">${label}</div>
    ${THEMES.filter(t => t.kind === kind).map(t => `
      <button role="menuitemradio" data-theme="${t.id}" aria-checked="false">
        <span class="swatch" style="background:${t.dot};box-shadow:inset 0 0 0 2px ${t.accent}"></span>
        ${t.name}
      </button>`).join('')}
  `).join('');
  menu.querySelectorAll('button').forEach(b => {
    b.onclick = () => { applyTheme(b.dataset.theme); menu.classList.remove('open'); };
  });
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (e) {}
  // 沒存過就跟著系統的深淺
  if (!saved && window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
    saved = 'paper';
  }
  buildThemeMenu();
  applyTheme(saved || 'midnight');

  const btn = $('#themebtn'), menu = $('#thememenu');
  btn.onclick = (e) => {
    e.stopPropagation();
    const open = menu.classList.toggle('open');
    btn.setAttribute('aria-expanded', String(open));
  };
  document.addEventListener('click', () => {
    menu.classList.remove('open');
    btn.setAttribute('aria-expanded', 'false');
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { menu.classList.remove('open'); btn.setAttribute('aria-expanded', 'false'); }
  });
}

function setSidebar(px, persist) {
  const width = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, Math.round(px)));
  document.documentElement.style.setProperty('--sidebar', width + 'px');
  if (persist) { try { localStorage.setItem(SIDEBAR_KEY, String(width)); } catch (e) {} }
  return width;
}

function initResizer() {
  const bar = $('#resizer');
  if (!bar) return;
  let saved = null;
  try { saved = Number(localStorage.getItem(SIDEBAR_KEY)); } catch (e) {}
  // 視窗太窄時不要沿用太寬的側欄
  const maxForViewport = Math.min(SIDEBAR_MAX, Math.round(window.innerWidth * 0.55));
  setSidebar(saved || Math.min(SIDEBAR_DEFAULT, maxForViewport), false);

  let dragging = false;
  const onMove = (e) => {
    if (!dragging) return;
    setSidebar(e.clientX, false);
  };
  const stop = () => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove('dragging');
    document.body.classList.remove('resizing');
    const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar'), 10);
    if (cur) setSidebar(cur, true);
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', stop);
  };
  bar.addEventListener('mousedown', (e) => {
    e.preventDefault();
    dragging = true;
    bar.classList.add('dragging');
    document.body.classList.add('resizing');
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', stop);
  });
  // 雙擊還原
  bar.addEventListener('dblclick', () => setSidebar(SIDEBAR_DEFAULT, true));
  // 鍵盤也能調（無障礙）
  bar.addEventListener('keydown', (e) => {
    const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar'), 10) || SIDEBAR_DEFAULT;
    if (e.key === 'ArrowLeft')  { e.preventDefault(); setSidebar(cur - 16, true); }
    if (e.key === 'ArrowRight') { e.preventDefault(); setSidebar(cur + 16, true); }
    if (e.key === 'Home')       { e.preventDefault(); setSidebar(SIDEBAR_DEFAULT, true); }
  });
  window.addEventListener('resize', () => {
    const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar'), 10) || SIDEBAR_DEFAULT;
    if (window.innerWidth > 820) setSidebar(Math.min(cur, Math.round(window.innerWidth * 0.55)), false);
  });
}

let CURRENT = null;      // 目前專案 payload
let SELECTED = new Set(); // 勾選的 index
let BUSY = false;

// ------------------------------------------------------------ 工具
function toast(msg, ms = 2200) {
  const el = $('#toast'); el.textContent = msg; el.classList.add('show');
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove('show'), ms);
}
let PROG_LINE = null;
function consoleLine(text, isErr) {
  const box = $('#console');
  box.style.display = 'block';
  // 以 \r 開頭 = 覆寫同一行的進度，不要一行行堆
  if (text.startsWith('\r')) {
    if (!PROG_LINE || !PROG_LINE.isConnected) {
      PROG_LINE = document.createElement('div');
      PROG_LINE.className = 'ln prog';
      box.appendChild(PROG_LINE);
    }
    PROG_LINE.textContent = text.slice(1);
    box.scrollTop = box.scrollHeight;
    return;
  }
  PROG_LINE = null;
  const div = document.createElement('div');
  div.className = 'ln' + (isErr ? ' err' : '');
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function tsToSec(s) {
  s = String(s).trim();
  if (s.includes(':')) {
    const p = s.split(':').map(Number);
    return p.length === 3 ? p[0]*3600 + p[1]*60 + p[2] : p[0]*60 + p[1];
  }
  return Number(s);
}
const LIC_CLASS = {free:'lic-free','free-attribution':'lic-free',check:'lic-check',unknown:'lic-unknown',none:'lic-unknown'};

async function api(path, opts) {
  const res = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts));
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}


// ------------------------------------------------------------ 素材庫面板
let LIB = [];          // 目前清單
let LIBDRAG = null;    // 拖曳中的資料夾

function libVisible() {
  const m = $('#libmodal');
  return m && m.classList.contains('show');
}

async function openLibrary() {
  $('#libmodal').classList.add('show');
  renderLibrary();
  // 排序模式存在伺服器，重新載入或換瀏覽器都一致
  try {
    const lib = await api('/api/library');
    if (lib && lib.sort) $('#libsort').value = lib.sort;
  } catch (e) { /* 讀不到就用預設 */ }
}

function closeLibrary() {
  $('#libmodal').classList.remove('show');
  LIBDRAG = null;
}

async function renderLibrary() {
  try { LIB = await api('/api/projects'); } catch (e) { LIB = []; }
  const grid = $('#libgrid');
  $('#libcount').textContent = LIB.length ? `${LIB.length} 個專案` : '';

  if (!LIB.length) {
    grid.innerHTML = `<div class="libempty" style="grid-column:1/-1">
      <div class="big">📁</div>還沒有素材專案。<br>在左邊打關鍵字搜一輪，就會出現在這裡。
    </div>`;
    return;
  }

  grid.innerHTML = LIB.map((p, i) => {
    const cur = CURRENT && CURRENT.name === p.name;
    return `
    <div class="libcard ${cur ? 'current' : ''} ${p.missing ? 'ismissing' : ''}"
         data-name="${esc(p.name)}" data-i="${i}" draggable="true" tabindex="0">
      <div class="lctitle">
        ${p.pinned ? '<span class="pinmark" title="已釘選">📌</span>' : ''}
        <span class="lctext">${esc(p.title || p.name)}</span>
      </div>
      <div class="lcmeta">
        <span>${p.count} 支</span>
        <span>${p.clips} 段</span>
        ${p.size ? `<span>${esc(p.size)}</span>` : ''}
        ${p.created ? `<span>${esc(p.created.slice(0, 10))}</span>` : ''}
      </div>
      <div class="lcqueries">${esc((p.queries || []).join('、') || '—')}</div>
      <div class="lcactions">
        <button class="mini" data-act="open">開啟</button>
        <button class="mini" data-act="rename">改名</button>
        <button class="mini" data-act="pin">${p.pinned ? '取消釘選' : '釘選'}</button>
      </div>
    </div>`;
  }).join('');

  grid.querySelectorAll('.libcard').forEach(card => {
    const name = card.dataset.name;
    card.onclick = (e) => {
      if (e.target.closest('[data-act]')) return;
      pickProject(name);
    };
    card.querySelectorAll('[data-act]').forEach(btn => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        const act = btn.dataset.act;
        if (act === 'open') pickProject(name);
        else if (act === 'rename') startRename(card, name);
        else if (act === 'pin') await togglePin(name);
      };
    });
    card.ondblclick = (e) => {
      if (e.target.closest('[data-act]')) return;
      startRename(card, name);
    };

    // 拖曳排序
    card.ondragstart = (e) => {
      LIBDRAG = name;
      card.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', name);
    };
    card.ondragend = () => {
      LIBDRAG = null;
      card.classList.remove('dragging');
      document.querySelectorAll('.libcard').forEach(c => c.classList.remove('drop-target'));
    };
    card.ondragover = (e) => {
      if (!LIBDRAG || LIBDRAG === name) return;
      e.preventDefault();
      card.classList.add('drop-target');
    };
    card.ondragleave = () => card.classList.remove('drop-target');
    card.ondrop = async (e) => {
      e.preventDefault();
      card.classList.remove('drop-target');
      if (!LIBDRAG || LIBDRAG === name) return;
      const order = LIB.map(x => x.name).filter(n => n !== LIBDRAG);
      const at = order.indexOf(name);
      order.splice(at < 0 ? order.length : at, 0, LIBDRAG);
      await saveOrder(order);
    };
  });
}

function pickProject(name) {
  closeLibrary();
  openProject(name);
}

async function togglePin(name) {
  const p = LIB.find(x => x.name === name);
  if (!p) return;
  await libAction('pin', {project: name, pinned: !p.pinned});
  toast(p.pinned ? '已取消釘選' : '已釘選，會排在最前面');
}

async function libAction(action, extra = {}) {
  const res = await api('/api/library', {
    method: 'POST',
    body: JSON.stringify({action, ...extra}),
  });
  if (res && res.projects) LIB = res.projects;
  await renderLibrary();
  return res;
}

async function saveOrder(order) {
  // 手動拖曳 = 使用者自己決定順序，排序模式要跟著回到 manual
  $('#libsort').value = 'manual';
  await libAction('order', {order});
}

function startRename(card, name) {
  const target = card.querySelector('.lctext');
  if (!target || card.querySelector('input.renaming')) return;
  const current = target.textContent;
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'renaming';
  input.value = current;
  input.maxLength = 120;
  target.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const commit = async () => {
    if (done) return;
    done = true;
    const title = input.value.trim();
    if (!title || title === current) { await renderLibrary(); return; }
    try {
      await libAction('rename', {project: name, title});
      toast('已改名為「' + title + '」');
    } catch (e) {
      toast('改名失敗：' + e.message);
      await renderLibrary();
    }
  };
  input.onblur = commit;
  input.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') { done = true; input.value = current; renderLibrary(); }
  };
}


// ------------------------------------------------------------ 掃描／復原
async function runScan() {
  const box = $('#libscanbox');
  box.innerHTML = '<div class="libscan">掃描中…</div>';
  let r;
  try { r = await api('/api/library/scan'); }
  catch (e) { box.innerHTML = `<div class="libscan">掃描失敗：${esc(e.message)}</div>`; return; }

  if (r.ok) {
    box.innerHTML = `<div class="libscan" style="color:var(--muted)">
      ✓ 掃描完成，記錄與 ${esc(r.root)} 一致（${r.on_disk.length} 個專案）</div>`;
    return;
  }

  const un = r.untracked || [];
  const mi = r.missing || [];
  box.innerHTML = `<div class="libscan">
    <h4>掃描結果</h4>
    ${un.length ? `<div>· 磁碟上有、記錄裡沒有：<b>${un.length}</b> 個</div>
      <div class="row2">
        ${un.map(x => `<span class="f">${esc(x.folder)}</span>`).join('')}
        <span class="grow"></span>
        <button class="mini" id="libadopt">全部納入</button>
      </div>` : ''}
    ${mi.length ? `<div style="margin-top:8px">· 記錄有、磁碟上找不到：<b>${mi.length}</b> 個
      <span style="color:var(--muted)">（可能是換了目錄或被移走）</span></div>
      <div class="row2">
        ${mi.map(x => `<span class="f">${esc(x.folder)}</span>`).join('')}
        <span class="grow"></span>
        <button class="mini ghost" id="libforget">移除這些空記錄</button>
      </div>` : ''}
  </div>`;

  const adopt = $('#libadopt');
  if (adopt) adopt.onclick = async () => {
    await api('/api/library', {method:'POST', body: JSON.stringify({
      action:'adopt', folders: un.map(x => x.folder)})});
    toast(`已納入 ${un.length} 個專案`);
    await renderLibrary();
    await runScan();
  };
  const forget = $('#libforget');
  if (forget) forget.onclick = async () => {
    if (!confirm(`確定要移除 ${mi.length} 筆記錄？\n（只移除記錄，不會動到任何檔案）`)) return;
    await api('/api/library', {method:'POST', body: JSON.stringify({
      action:'forget', folders: mi.map(x => x.folder)})});
    toast('已移除空記錄');
    await renderLibrary();
    await runScan();
  };
}

function initLibrary() {
  $('#openlib').onclick = openLibrary;
  $('#libclose').onclick = closeLibrary;
  $('#libscan').onclick = runScan;
  const modal = $('#libmodal');
  modal.onclick = (e) => { if (e.target === modal) closeLibrary(); };
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && libVisible()) closeLibrary();
  });
  $('#libsort').onchange = async (e) => {
    const mode = e.target.value;
    await libAction('sort', {mode});
    toast(mode === 'manual' ? '已切回手動順序' : '已依「' + e.target.selectedOptions[0].text + '」排序');
  };
}

// ------------------------------------------------------------ 專案列表
async function loadProjects() {
  const list = await api('/api/projects');
  const box = $('#projects');
  if (!list.length) { box.innerHTML = '<div class="hint">還沒有專案</div>'; return; }
  box.innerHTML = list.map(p => `
    <div class="proj ${CURRENT && CURRENT.name === p.name ? 'active' : ''}" data-name="${esc(p.name)}"
         title="${esc((p.queries || []).join('、'))}">
      <span>${p.pinned ? '📌 ' : ''}${esc(p.title || p.name)}</span>
      <small>${p.count}支 / ${p.clips}段</small>
    </div>`).join('');
  box.querySelectorAll('.proj').forEach(el => el.onclick = () => openProject(el.dataset.name));
}

// ------------------------------------------------------------ 開專案
async function openProject(name) {
  try {
    CURRENT = await api('/api/project/' + encodeURIComponent(name));
    SELECTED = new Set();
    render();
    loadProjects();
  } catch (e) { toast('開不起來：' + e.message); }
}

/** 靜默刷新：下載進行中定時更新，讓新片段與截圖馬上出現。
 *  不能直接用 openProject——它會清掉使用者已勾選的項目。 */
async function silentRefresh() {
  if (!CURRENT || BUSY) return;
  try {
    const fresh = await api('/api/project/' + encodeURIComponent(CURRENT.name));
    const before = JSON.stringify(CURRENT.stats || {});
    CURRENT = fresh;
    const after = JSON.stringify(CURRENT.stats || {});
    // 只有真的變了才重繪，避免把正在捲動的清單跳回頂端
    if (before !== after) render();
    else updateItemClips(fresh);
  } catch (_) { /* 下載中偶爾讀不到是正常的，下一輪再試 */ }
}

/** 逐支比對片段數，只更新有變的那幾張卡，不動整體版面。 */
function updateItemClips(fresh) {
  const items = fresh.items || [];
  let changed = false;
  for (const it of items) {
    const idx = (CURRENT.items || []).findIndex(x => x.vid === it.vid && x.platform === it.platform);
    if (idx < 0) continue;
    const old = CURRENT.items[idx];
    const oldN = (old.clips || []).length;
    const newN = (it.clips || []).length;
    if (oldN !== newN) { changed = true; break; }
  }
  if (changed) render();
}

function render() {
  if (!CURRENT) return;
  const s = CURRENT.stats;
  // 授權統計：用中文標籤，把「未知」獨立成一句灰字，不要佔一排膠囊
  const LIC_LABEL = {free:'可自由使用', 'free-attribution':'可用需標註',
                     check:'需確認授權', unknown:'未知', none:'無資訊'};
  const licEntries = Object.entries(s.by_license || {});
  const knownLic = licEntries.filter(([k]) => k !== 'unknown' && k !== 'none');
  const unknownCount = licEntries
    .filter(([k]) => k === 'unknown' || k === 'none')
    .reduce((a, [, v]) => a + v, 0);
  const lic = knownLic.map(([k, v]) =>
    `<span class="pill ${LIC_CLASS[k] || 'lic-unknown'}">${esc(LIC_LABEL[k] || k)} · ${v}</span>`).join('')
    + (unknownCount ? `<span class="licnote">${unknownCount} 支未標示授權</span>` : '');
  const items = CURRENT.items || [];

  const cards = items.map(it => {
    const sel = SELECTED.has(it.index);
    const thumbs = it.thumbs.length
      ? `<div class="thumbs">${it.thumbs.map(t => `<img src="${esc(t)}" loading="lazy" alt="影片畫面" onclick="lightbox('img','${esc(t)}')">`).join('')}</div>`
      : '';
    const clips = it.clips.length
      ? `<div class="clipbox">${it.clips.map((c, ci) => `
          <div class="clip-row">
            ${c.thumb
              ? `<img class="clip-thumb" src="${esc(c.thumb)}" loading="lazy"
                      alt="${esc(c.name)} 的畫面預覽" title="這一格就是這個片段"
                      onclick="lightbox('img','${esc(c.thumb)}')">`
              : `<span class="clip-thumb clip-nothumb" title="沒有縮圖">無圖</span>`}
            <a href="${esc(c.rel)}" data-play="${esc(c.rel)}" data-name="${esc(c.name)}">${esc(c.name)}</a>
            <code>${esc(c.span)}</code>
            <span>${esc(c.dims)} · ${esc(c.size)}</span>
            <span class="clip-actions">
              <button class="mini" data-play="${esc(c.rel)}" data-name="${esc(c.name)}">▶ 播放</button>
              <a class="mini" href="${esc(c.rel)}" download>下載</a>
            </span>
          </div>`).join('')}</div>`
      : '';
    const licenseCls = LIC_CLASS[it.license_kind] || 'lic-unknown';
    // 「未知」幾乎每支都是，掛在每張卡片上等於雜訊 → 只有真的查出授權才顯示膠囊
    const showLicense = it.license_kind && it.license_kind !== 'unknown';
    const notes = [];
    if (it.repost) notes.push('轉載／非原始出處');
    if (it.note) notes.push(it.note);
    if (it.error) notes.push(it.error);
    return `
      <div class="card ${sel ? 'sel' : ''}" data-idx="${it.index}">
        <input type="checkbox" ${sel ? 'checked' : ''} data-idx="${it.index}">
        <div>
          <h3><a href="${esc(it.url)}" target="_blank" rel="noopener">${it.index + 1}. ${esc(it.title)}</a></h3>
          <div class="meta">
            <span>${esc(it.platform_label)}</span>
            <span>${esc(it.author || '—')}</span>
            <span>時長 <b>${esc(it.duration)}</b></span>
            <span>觀看 <b>${esc(it.views_label)}</b></span>
            <span>${esc(it.pubdate || '—')}</span>
            ${showLicense ? `<span class="pill ${licenseCls}">${esc(it.license_short)}</span>` : ''}
          </div>
          ${thumbs}
          ${notes.length ? `<div class="warnline">⚠ ${esc(notes.join('；'))}</div>` : ''}
          ${clips}
        </div>
      </div>`;
  }).join('');

  $('#view').innerHTML = `
    <header class="bar">
      <h2>${esc((CURRENT.queries || []).join('、'))}</h2>
      <span class="sub" style="margin:0">${items.length} 支候選 · ${s.with_clips} 段已切 · ${esc(CURRENT.clips_bytes)}</span>
    </header>
    <div class="pills">
      <span class="pill">${esc((CURRENT.sources || []).join(' + '))}</span>
      ${lic}
    </div>
    <div class="toolbar">
      <label style="display:flex;gap:6px;align-items:center;font-size:12.5px;color:var(--muted)">
        <input type="checkbox" id="selall"> 全選
      </label>
      <span class="sp"></span>
      <select id="mode" aria-label="切片方式" style="width:132px">
        <option value="auto">切片（每段固定）</option>
        <option value="range">切片（自訂區段）</option>
        <option value="full">整支下載</option>
      </select>
      <span class="sub" id="perlabel" style="margin:0">每段</span>
      <input type="number" id="per" value="15" min="1" step="1" aria-label="每段秒數" style="width:66px">
      <span class="sub" id="persuffix" style="margin:0">秒</span>
      <input type="text" id="starts" placeholder="指定秒數，如 90, 3:20" style="width:170px">
      <input type="text" id="ranges" placeholder="區段，如 1:00-2:30, 5:00-6:00" style="width:230px;display:none">
      <select id="height" aria-label="解析度" style="width:104px">
        <option value="2160">4K</option>
        <option value="1440">1440p</option>
        <option value="1080" selected>1080p</option>
        <option value="720">720p</option>
      </select>
      <label style="display:flex;gap:5px;align-items:center;font-size:12.5px;color:var(--muted)">
        <input type="checkbox" id="frames" checked> 抓畫面
      </label>
      <label style="display:flex;gap:5px;align-items:center;font-size:12.5px;color:var(--muted)">
        <input type="checkbox" id="audio"> 只要聲音
      </label>
      <label style="display:flex;gap:5px;align-items:center;font-size:12.5px;color:var(--muted)"
             title="勾起來：這次用的下載參數會存成預設值">
        <input type="checkbox" id="savesettings"> 記住這些設定
      </label>
      <button class="primary" id="grab" style="width:auto;padding:8px 18px">切片段</button>
      <button class="ghost tiny" id="refresh">重新確認授權</button>
      <button class="ghost tiny" id="openlist">開 HTML 清單</button>
    </div>
    ${items.length ? cards : '<div class="empty">這個專案沒有結果。</div>'}`;

  const cardsEl = document.querySelectorAll('.card input[type=checkbox]');
  cardsEl.forEach(cb => {
    cb.onchange = () => {
      const idx = Number(cb.dataset.idx);
      if (cb.checked) SELECTED.add(idx); else SELECTED.delete(idx);
      cb.closest('.card').classList.toggle('sel', cb.checked);
    };
  });
  $('#selall').onchange = (e) => {
    SELECTED = e.target.checked ? new Set(items.map(i => i.index)) : new Set();
    render();
  };
  const mode = $('#mode');
  const syncMode = () => {
    const m = mode.value;
    $('#ranges').style.display = m === 'range' ? '' : 'none';
    $('#starts').style.display = m === 'auto' ? '' : 'none';
    $('#per').style.display = m === 'full' ? 'none' : '';
    $('#perlabel').style.display = m === 'full' ? 'none' : '';
    $('#persuffix').style.display = m === 'full' ? 'none' : '';
  };
  mode.onchange = syncMode;
  syncMode();
  $('#grab').onclick = grabClips;
  $('#refresh').onclick = refreshProject;
  $('#openlist').onclick = () => api('/api/open', {method:'POST', body: JSON.stringify({project: CURRENT.name})});
}


// ------------------------------------------------------------ 播放已下載的片段
let PLAYING = null;   // 目前播放的檔案路徑

function playClip(rel, name) {
  const box = $('#player'), vid = $('#videoplayer');
  if (PLAYING === rel) {            // 再按一次就收起
    closePlayer();
    return;
  }
  PLAYING = rel;
  box.style.display = '';
  $('#playername').textContent = name || rel.split('/').pop();
  $('#playermeta').textContent = '';
  vid.src = rel;
  vid.play().catch(() => { /* 瀏覽器可能擋自動播放，讓使用者自己按 */ });
  $('#playerdownload').href = rel;

  // 顯示這支影片的幀圖（如果有）
  const frames = $('#playerframes');
  frames.innerHTML = '';
  const item = (CURRENT && CURRENT.items || []).find(it =>
    (it.clips || []).some(c => c.rel === rel));
  const thumbList = (item && item.thumbs) || [];
  if (thumbList.length) {
    frames.innerHTML = thumbList.map(t => `<img src="${esc(t)}" alt="畫面節點" onclick="seekToFrame('${esc(t)}')">`).join('');
  }

  // 標記清單中正在播放的那一列
  document.querySelectorAll('[data-play]').forEach(el => {
    el.classList.toggle('on', el.dataset.play === rel);
  });

  vid.onloadedmetadata = () => {
    const d = vid.duration;
    $('#playermeta').textContent = `${fmtSec(d)} · ${vid.videoWidth}×${vid.videoHeight}`;
  };
  box.scrollIntoView({behavior:'smooth', block:'nearest'});
}

// 點幀圖 → 跳到那個時間點（近似：平均分配在片長裡）
function seekToFrame(src) {
  const vid = $('#videoplayer');
  const item = (CURRENT && CURRENT.items || []).find(it =>
    (it.thumbs || []).some(t => t === src));
  if (!item || !vid.duration) return;
  const idx = item.thumbs.indexOf(src);
  const total = item.thumbs.length || 1;
  // 幀圖是從整支影片抓的，這裡依比例換算
  vid.currentTime = (vid.duration * idx) / Math.max(1, total - 1 || 1);
  vid.pause();
}

function fmtSec(s) {
  if (!isFinite(s)) return '—';
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

function closePlayer() {
  const vid = $('#videoplayer');
  vid.pause(); vid.removeAttribute('src'); vid.load();
  $('#player').style.display = 'none';
  PLAYING = null;
  document.querySelectorAll('[data-play]').forEach(el => el.classList.remove('on'));
}

function initPlayer() {
  $('#playerclose').onclick = closePlayer;
  $('#videoplayer').addEventListener('error', () => {
    $('#playermeta').textContent = '這個格式瀏覽器可能不支援，請按「下載這個檔案」';
  });
  // 事件委派：影片清單是動態重繪的，不能在元素上綁
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-play]');
    if (el) {
      e.preventDefault();
      playClip(el.dataset.play, el.dataset.name);
      return;
    }
    // 點播放器以外的任何地方就收起來。
    // 播放器是內嵌面板（不是遮罩），所以「外面」= 不在 #player 裡。
    if (!PLAYING) return;
    if (e.target.closest('#player')) return;   // 面板自己的按鈕、進度條
    if (e.target.closest('#lightbox')) return; // 大圖模式另外處理
    // 拖曳選字不算是「點外面」
    const sel = window.getSelection();
    if (sel && String(sel).length > 0) return;
    closePlayer();
  });
}

function lightbox(kind, src) {
  const box = $('#lightbox');
  box.innerHTML = kind === 'img'
    ? `<img src="${src}" alt="放大檢視">`
    : `<video src="${src}" controls autoplay></video>`;
  box.classList.add('show');
  box.onclick = () => { box.classList.remove('show'); box.innerHTML = ''; };
}

// ------------------------------------------------------------ 下載進度
let DLCARD_ON = false;
let DL_STAMP = 0;   // 用來讓「完成後自動收起」的定時器知道有沒有被新進度打斷

function showProgressBar(pct) {
  const bar = $('#progressbar'), fill = $('#progressfill');
  bar.classList.add('on');
  if (pct !== null) fill.style.width = pct + '%';
}

/** 一支影片的細進度 + 整個作業的粗進度，都畫在同一張卡上。
 *  只畫細進度看不出「3 支裡面下到第幾支」，所以兩條都要。 */
function updateDownload(p) {
  const card = $('#dlcard');
  if (!p || p.kind !== 'download') return;
  card.classList.add('on');
  document.body.classList.add('dl-on');
  DLCARD_ON = true;

  const pct = p.percent === null || p.percent === undefined ? null : Number(p.percent);
  $('#dlname').textContent = p.title || '';
  $('#dlpct').textContent = pct === null ? '…' : Math.round(pct) + '%';
  $('#dlfill').style.width = (pct === null ? 0 : Math.min(100, pct)) + '%';

  // 作業層級：position / total，例如「第 2 / 3 支」
  const total = Number(p.total) || 0;
  const pos = Number(p.position) || 0;
  if (total > 0) {
    $('#dlbadge').textContent = total > 1 ? `第 ${pos} / ${total} 支` : '下載中';
    const overall = total > 1 ? ((pos - 1) + (pct === null ? 0 : pct / 100)) / total * 100 : 0;
    $('#dltotalfill').style.width = Math.max(0, Math.min(100, overall)) + '%';
  } else {
    $('#dlbadge').textContent = '下載中';
    $('#dltotalfill').style.width = pct === null ? 0 : Math.min(100, pct) + '%';
  }

  const bits = [];
  if (p.speed) bits.push(p.speed);
  if (p.fragment) bits.push('分段 ' + p.fragment);
  $('#dlspeed').textContent = bits.join(' · ');
  $('#dleta').textContent = p.eta ? '剩 ' + p.eta : '';

  // 頂部細條保留，讓人在滾動時也知道有事在跑
  showProgressBar(total > 1 ? ((pos - 1) + (pct === null ? 0 : pct / 100)) / total * 100 : pct);
}

function finishDownload() {
  if (!DLCARD_ON) { $('#progressbar').classList.remove('on'); return; }
  $('#dlfill').style.width = '100%';
  $('#dltotalfill').style.width = '100%';
  $('#dlpct').textContent = '100%';
  $('#dlbadge').textContent = '完成';
  $('#dlspeed').textContent = '';
  $('#dleta').textContent = '';
  $('#progressfill').style.width = '100%';
  // 這個作業可能還有多支影片要下載，下一支會馬上把卡片打開。
  // 定時器到的時候如果已經有新進度進來，就不要把它關掉
  // （真的發生過：卡片在下載中變成 display:none，進度還在跑卻看不到）。
  const stamp = ++DL_STAMP;
  setTimeout(() => {
    if (DL_STAMP !== stamp) return;  // 已經開始下一支了
    hideDownload();
    $('#progressbar').classList.remove('on');
    $('#progressfill').style.width = '0%';
  }, 1200);
}

function hideDownload() {
  DLCARD_ON = false;
  $('#dlcard').classList.remove('on');
  document.body.classList.remove('dl-on');
  $('#dlfill').style.width = '0%';
  $('#dltotalfill').style.width = '0%';
}

// ------------------------------------------------------------ 執行工作
async function follow(jobId, onDone) {
  return new Promise((resolve, reject) => {
    const es = new EventSource('/api/stream/' + jobId);
    // 下載進行中每 2.5 秒靜默刷新一次：片段一落地就出現，不用等整個作業跑完
    let poll = null;
    const startPolling = () => {
      if (poll) return;
      poll = setInterval(() => { silentRefresh(); }, 2500);
    };
    const stopPolling = () => { if (poll) { clearInterval(poll); poll = null; } };
    es.addEventListener('log', e => {
      const msg = JSON.parse(e.data);
      if (msg.startsWith('\r')) {
        // 進度行可能有結構化資料：\x00 後面是 JSON
        const nul = msg.indexOf('\x00');
        if (nul > 0) {
          try {
            updateDownload(JSON.parse(msg.slice(nul + 1)));
            startPolling();
          } catch (_) {}
          consoleLine(msg.slice(0, nul));
          return;
        }
        // 沒有結構化資料的舊式進度行：退回解析百分比
        const m = msg.match(/(\d+(?:\.\d+)?)%/);
        showProgressBar(m ? Math.min(100, Number(m[1])) : null);
      }
      consoleLine(msg);
    });
    es.addEventListener('done', e => {
      es.close(); stopPolling(); consoleLine('✔ 完成');
      finishDownload();
      const data = JSON.parse(e.data);
      resolve(data);
    });
    es.addEventListener('error', e => {
      es.close(); stopPolling();
      hideDownload();
      $('#progressbar').classList.remove('on');
      let msg = '發生錯誤';
      try { msg = JSON.parse(e.data); } catch (_) {}
      consoleLine('✗ ' + msg, true);
      reject(new Error(msg));
    });
  });
}

async function search() {
  if (BUSY) return;
  const raw = $('#keywords').value.trim();
  if (!raw) { toast('先輸入關鍵字'); return; }
  const keywords = raw.split(/[\n,，]+/).map(s => s.trim()).filter(Boolean);
  const body = {
    keywords,
    platform: $('#platform').value,
    sort: $('#sort').value,
    per_keyword: Number($('#per_keyword').value) || 12,
    min_duration: Number($('#min_duration').value) || 0,
    max_duration: Number($('#max_duration').value) || 0,
    fresh_days: Number($('#fresh_days').value) || 0,
    min_views: Number($('#min_views').value) || 0,
    exclude: $('#exclude').value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
    only_free: $('#only_free').checked,
    drop_reposts: $('#drop_reposts').checked,
    verify_license: $('#verify_license').checked,
  };
  busy(true, '搜尋中');
  consoleLine('▶ 搜尋：' + keywords.join('、'));
  try {
    const {job} = await api('/api/search', {method:'POST', body: JSON.stringify(body)});
    const res = await follow(job);
    toast(`找到 ${res.count} 支`);
    await loadProjects();
    await openProject(res.project);
  } catch (e) {
    toast(e.message, 4000);
  } finally { busy(false); }
}

async function grabClips() {
  if (BUSY || !CURRENT) return;
  if (!SELECTED.size) { toast('先勾選要的影片'); return; }
  const starts = $('#starts').value.split(/[,，]/).map(s => s.trim()).filter(Boolean)
    .map(tsToSec).filter(n => !isNaN(n));
  const mode = $('#mode').value;
  const rangeSpec = $('#ranges').value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
  const ranges = rangeSpec.map(spec => {
    const m = spec.split(/[-~]/);
    return m.length === 2 ? [tsToSec(m[0]), tsToSec(m[1])] : null;
  }).filter(Boolean);
  if (mode === 'range' && !ranges.length) { toast('請填區段，例如 1:00-2:30'); return; }

  const body = {
    project: CURRENT.name,
    indexes: [...SELECTED],
    per: Number($('#per').value) || 15,
    starts: mode === 'auto' ? starts : [],
    ranges: mode === 'range' ? ranges : [],
    full: mode === 'full',
    height: Number($('#height').value) || 1080,
    frames: $('#frames').checked,
    audio: $('#audio').checked,
    save_settings: $('#savesettings') && $('#savesettings').checked,
  };
  busy(true, '下載中');
  consoleLine(mode === 'full'
    ? `▶ 整支下載 ${body.indexes.length} 支（${body.height}p）`
    : `▶ 下載 ${body.indexes.length} 支，${mode === 'range' ? ranges.length + ' 個區段' : '每段 ' + body.per + ' 秒'}`);
  try {
    const {job} = await api('/api/pick', {method:'POST', body: JSON.stringify(body)});
    const res = await follow(job);
    toast(`完成 ${res.grabbed} 段`);
    await loadProjects();
    await openProject(CURRENT.name);
  } catch (e) {
    toast(e.message, 4000);
  } finally { busy(false); }
}

async function refreshProject() {
  if (BUSY || !CURRENT) return;
  busy(true, '確認中');
  try {
    const {job} = await api('/api/refresh', {method:'POST', body: JSON.stringify({project: CURRENT.name})});
    await follow(job);
    toast('授權已更新');
    await openProject(CURRENT.name);
  } catch (e) { toast(e.message, 4000); } finally { busy(false); }
}

function busy(on, label) {
  BUSY = on;
  const btn = $('#go');
  btn.disabled = on;
  btn.innerHTML = on ? `<span class="spinner"></span>${label}…` : '搜尋素材';
}

// ------------------------------------------------------------ B 站登入
async function checkAuth() {
  try {
    const who = await api('/api/login/status');
    const box = $('#authbox'), label = $('#authwho'), btn = $('#loginbtn');
    if (who.logged_in) {
      box.classList.add('ok');
      label.textContent = 'B 站：' + (who.name || who.mid) + (who.vip ? ' · 大會員' : '');
      btn.textContent = '重新登入';
    } else {
      box.classList.remove('ok');
      label.textContent = 'B 站未登入（高畫質／會員片會失敗）';
      btn.textContent = '掃碼登入';
    }
  } catch (e) { /* 忽略 */ }
}

async function startLogin() {
  const modal = $('#loginmodal');
  $('#loginstat').textContent = '取得 QR…';
  modal.classList.add('show');
  try {
    const res = await api('/api/login/start', {method:'POST', body:'{}'});
    if (res.already) {
      $('#loginstat').textContent = '已經登入了：' + (res.already.name || '');
      setTimeout(() => modal.classList.remove('show'), 1200);
      checkAuth();
      return;
    }
    $('#qrimg').src = res.image + '?t=' + Date.now();
    $('#loginstat').textContent = '等待掃碼…';
    pollLogin(res.key);
  } catch (e) {
    $('#loginstat').textContent = '失敗：' + e.message;
  }
}

async function pollLogin(key) {
  const deadline = Date.now() + 185000;
  const tick = async () => {
    if (Date.now() > deadline) {
      $('#loginstat').textContent = 'QR 過期了，關掉再按一次登入';
      return;
    }
    try {
      const res = await api('/api/login/poll', {method:'POST', body: JSON.stringify({key})});
      $('#loginstat').textContent = res.message;
      if (res.status === 'ok') {
        const who = res.who || {};
        $('#loginstat').textContent = '✔ 登入成功：' + (who.name || '');
        setTimeout(() => $('#loginmodal').classList.remove('show'), 1400);
        checkAuth();
        return;
      }
      if (res.status === 'expired') return;
    } catch (e) { /* 繼續試 */ }
    setTimeout(tick, 2000);
  };
  tick();
}

$('#loginbtn').onclick = startLogin;
$('#loginmodal').onclick = (e) => { if (e.target.id === 'loginmodal') e.currentTarget.classList.remove('show'); };
checkAuth();

$('#go').onclick = search;
$('#keywords').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) search();
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  // 由外而內收：先關大圖，再關播放器
  const lb = $('#lightbox');
  if (lb.classList.contains('show')) {
    lb.classList.remove('show'); lb.innerHTML = '';
    return;
  }
  if (PLAYING) closePlayer();
});
async function showBuild() {
  try {
    const v = await api('/api/version');
    $('#buildtag').textContent = 'build ' + v.build;
  } catch (e) { /* 忽略 */ }
}
async function boot() {
  initTheme();
  initResizer();
  initPlayer();
  initLibrary();
  showBuild();
  await loadProjects();
  try {
    const list = await api('/api/projects');
    if (list.length) openProject(list[0].name);
  } catch (e) { /* 沒有專案就算了 */ }
}
boot();
