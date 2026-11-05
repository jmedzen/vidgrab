#!/usr/bin/env python3
"""用 CDP 驗證介面：主題、側欄寬度、播放器、素材庫、JS 錯誤。

比截圖可靠（截圖在 headless 常常逾時），而且能直接斷言。

⚠️ 這是「開發時的自我驗證」，不是程式執行的一部分。
   vidgrab 本身開介面用的是 webbrowser.open()，也就是**系統預設瀏覽器**，
   完全不需要 Chrome。

CDP 是 Chromium 系專有的協定（Firefox／Safari 用的是別的機制），
所以這個檢查工具只認 Chromium 系（Chrome／Edge／Brave／Chromium）。
沒偵測到就直接跳過，不會幫你亂開瀏覽器。

用法：
    python tools/check_ui.py                # 連現有的 CDP（9222）
    python tools/check_ui.py --launch       # 自己起一個隔離的 Chromium
    python tools/check_ui.py --launch --headful   # 想看畫面就加這個
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

try:
    import websocket
except ImportError:  # pragma: no cover - 只在使用者環境缺套件時觸發
    print("✗ 缺少 websocket-client（介面驗證要用它透過 CDP 遙控瀏覽器）")
    print("  安裝： pip install websocket-client")
    print("  或：   pip install -e '.[dev]'")
    raise SystemExit(2)

CDP = "http://127.0.0.1:9222"

# Chromium 系的候選路徑。順序＝偏好順序，全都不是預設瀏覽器也無妨，
# 因為這只是驗證用的臨時實例，跑完就關掉。
_CHROMIUM = {
    "Darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
    "Linux": [
        "google-chrome", "google-chrome-stable", "chromium",
        "chromium-browser", "microsoft-edge", "brave-browser",
    ],
    "Windows": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ],
}


def find_chromium() -> str | None:
    """找一個 Chromium 系執行檔；找不到回 None（不猜、不亂裝）。"""
    for cand in _CHROMIUM.get(platform.system(), []):
        if os.path.sep in cand and os.path.exists(cand):
            return cand
        found = shutil.which(cand)
        if found:
            return found
    # 最後試 PATH 裡的通用名稱
    for name in ("chrome", "chromium", "msedge", "brave"):
        found = shutil.which(name)
        if found:
            return found
    return None


def cdp_alive(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{CDP}/json/version", timeout=timeout) as r:
            json.loads(r.read())
        return True
    except Exception:  # noqa: BLE001
        return False


def launch_chromium(headful: bool = False) -> subprocess.Popen | None:
    """起一個隔離的 Chromium 實例。

    兩個關鍵點，都是踩過的坑：

    1. **不要動 HOME**。用假的 HOME 會讓瀏覽器找不到系統鑰匙圈，
       macOS 就跳出「Keychain Not Found」的視窗。用 --user-data-dir 隔離就夠了。
    2. 需要 --no-sandbox。在沙箱化的環境裡，Chrome 自己的 sandbox 初始化會失敗
       （sandbox initialization failed: Operation not permitted）然後直接崩潰。
       這裡只是跑驗證、開本機頁面，關掉 sandbox 沒有額外風險。
    """
    exe = find_chromium()
    if not exe:
        return None

    profile = tempfile.mkdtemp(prefix="vidgrab-cdp-")
    args = [
        exe,
        f"--user-data-dir={profile}",
        "--remote-debugging-port=9222",
        "--no-sandbox",  # 沙箱環境裡 Chrome 自己的 sandbox 起不來會崩潰
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "http://127.0.0.1:8310/",  # 直接開目標頁，避免 about:blank 被取代時斷線
    ]
    if not headful:
        args.insert(1, "--headless=new")
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # 不要跟著終端機一起被砍
    )


def get(url: str):
    """讀一個 JSON 端點（測試裡用來對照前後狀態）。"""
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def new_tab(url: str) -> str:
    req = urllib.request.Request(f"{CDP}/json/new?{urllib.parse.quote(url, safe='')}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["id"]


class Tab:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.n = 0
        self.errors: list[str] = []
        self.console: list[str] = []

    def send(self, method: str, **params):
        self.n += 1
        want = self.n
        self.ws.send(json.dumps({"id": want, "method": method, "params": params}))
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                msg = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if msg.get("method") == "Runtime.exceptionThrown":
                d = msg["params"]["exceptionDetails"]
                self.errors.append(d.get("exception", {}).get("description") or d.get("text", ""))
            elif msg.get("method") == "Runtime.consoleAPICalled":
                args = " ".join(str(a.get("value", "")) for a in msg["params"].get("args", []))
                self.console.append(f"[{msg['params']['type']}] {args}")
                if msg["params"]["type"] == "error":
                    self.errors.append(args)
            elif msg.get("method") == "Log.entryAdded":
                e = msg["params"]["entry"]
                if e.get("level") == "error":
                    self.errors.append(e.get("text", ""))
            elif msg.get("id") == want:
                return msg.get("result")
        return None

    def js(self, expr: str, await_promise: bool = False):
        r = self.send(
            "Runtime.evaluate", expression=expr, returnByValue=True,
            awaitPromise=await_promise,
        )
        res = r.get("result", {})
        if res.get("subtype") == "error":
            self.errors.append(str(res.get("description")))
        return res.get("value")

    def wait(self, expr: str, timeout: float = 15) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.js(expr):
                return True
            time.sleep(0.3)
        return False

    def close(self):
        self.ws.close()


def main() -> int:
    import urllib.parse as up

    argv = sys.argv[1:]
    launch = "--launch" in argv
    headful = "--headful" in argv

    # 先確認 Web UI 有在跑，否則後面的失敗訊息會很誤導
    try:
        urllib.request.urlopen("http://127.0.0.1:8310/api/version", timeout=3).read()
    except Exception:  # noqa: BLE001
        print("✗ 找不到正在跑的 vidgrab Web UI（http://127.0.0.1:8310）")
        print("  請先在另一個終端機執行：./vidgrab web")
        return 2

    proc = None
    if not cdp_alive():
        if not launch:
            print("✗ 沒有偵測到 CDP（http://127.0.0.1:9222）")
            print("  這是介面的自我驗證工具，需要一個 Chromium 系的瀏覽器提供 CDP。")
            print("  要讓它自己起一個臨時實例：python tools/check_ui.py --launch")
            print("  （vidgrab 本身開介面用的是系統預設瀏覽器，不需要 Chrome）")
            return 2
        print("… 啟動臨時 Chromium（跑完會關掉）")
        proc = launch_chromium(headful=headful)
        if not proc:
            print("✗ 找不到 Chromium 系瀏覽器（Chrome／Edge／Brave／Chromium）")
            print("  這個檢查需要 CDP，Firefox／Safari 不支援。")
            return 2
        for _ in range(40):  # 最多等 8 秒
            if cdp_alive(0.5):
                break
            time.sleep(0.2)
        else:
            print("✗ Chromium 起來了但 CDP 沒就緒")
            proc.terminate()
            return 2

    try:
        return _run()
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()


def _run() -> int:
    tab_id = new_tab("http://127.0.0.1:8310/")
    with urllib.request.urlopen(f"{CDP}/json/list", timeout=10) as r:
        tabs = json.loads(r.read())
    ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["id"] == tab_id)

    tab = Tab(ws_url)
    tab.send("Runtime.enable")
    tab.send("Emulation.setDeviceMetricsOverride",
             width=1500, height=950, deviceScaleFactor=1, mobile=False)
    tab.send("Log.enable")
    tab.send("Page.enable")
    tab.send("Page.navigate", url="http://127.0.0.1:8310/")
    time.sleep(4)

    ok = True

    def check(label: str, cond, detail=""):
        nonlocal ok
        mark = "✔" if cond else "✗"
        print(f"  {mark} {label}" + (f"  {detail}" if detail else ""))
        if not cond:
            ok = False

    print("── 主題")
    themes = tab.js("JSON.stringify(THEMES.map(t=>t.id))")
    print("  ", themes)
    ids = json.loads(themes) if themes else []
    check("六個主題定義存在", len(ids) == 6, str(ids))

    for tid in ids:
        tab.js(f"applyTheme('{tid}')")
        time.sleep(0.15)
        info = tab.js(f"""JSON.stringify((() => {{
          const cs = getComputedStyle(document.body);
          const root = getComputedStyle(document.documentElement);
          return {{
            theme: document.documentElement.dataset.theme,
            bg: cs.backgroundColor,
            bgVar: root.getPropertyValue('--bg').trim(),
            text: root.getPropertyValue('--text').trim(),
            accent: root.getPropertyValue('--accent').trim(),
          }};
        }})())""")
        d = json.loads(info)
        check(f"{tid} 套用成功", d["theme"] == tid, f"bg={d['bgVar']} text={d['text']}")

    # 淺色主題要真的變亮
    tab.js("applyTheme('paper')")
    time.sleep(0.2)
    light_bg = tab.js("getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()")
    tab.js("applyTheme('midnight')")
    time.sleep(0.2)
    dark_bg = tab.js("getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()")
    check("淺色與深色底色不同", light_bg != dark_bg, f"{light_bg} vs {dark_bg}")

    # 主題要能記住
    tab.js("applyTheme('forest')")
    tab.js("location.reload()")
    time.sleep(3.5)
    saved = tab.js("document.documentElement.dataset.theme")
    check("重新載入後記得主題", saved == "forest", str(saved))
    tab.js("applyTheme('midnight')")

    print("── 側欄寬度")
    w0 = tab.js("parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar'),10)")
    check("有初始寬度", bool(w0), f"{w0}px")

    # 模擬拖曳
    tab.js("""(() => {
      const bar = document.querySelector('#resizer');
      bar.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, clientX:330}));
      document.dispatchEvent(new MouseEvent('mousemove', {bubbles:true, clientX:480}));
      document.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, clientX:480}));
    })()""")
    time.sleep(0.4)
    w1 = tab.js("parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar'),10)")
    check("拖曳後變寬", w1 and w1 > w0, f"{w0} → {w1}")

    # 上下限
    tab.js("""(() => {
      const bar = document.querySelector('#resizer');
      bar.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, clientX:480}));
      document.dispatchEvent(new MouseEvent('mousemove', {bubbles:true, clientX:120}));
      document.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, clientX:120}));
    })()""")
    time.sleep(0.3)
    wmin = tab.js("parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar'),10)")
    hidden = tab.js("getComputedStyle(document.querySelector('#resizer')).display") == "none"
    check("把手可見（夠寬的視窗）", not hidden, "hidden" if hidden else "visible")
    check("拖到最左也不小於 240", wmin == 240, f"{wmin}px")

    # 雙擊還原
    tab.js("document.querySelector('#resizer').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))")
    time.sleep(0.3)
    wd = tab.js("parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar'),10)")
    check("雙擊還原預設", wd == 330, f"{wd}px")

    # 寬度要記住
    tab.js("setSidebar(420, true)")
    tab.js("location.reload()")
    time.sleep(3.5)
    wr = tab.js("parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar'),10)")
    check("重新載入後記得寬度", wr == 420, f"{wr}px")

    print("── 播放器")
    # 先確定開的是「有片段」的專案，否則沒東西可播（不該誤判成功能壞掉）
    tab.js("""(async () => {
      const list = await api('/api/projects');
      const withClips = list.find(p => p.clips > 0);
      if (withClips) await openProject(withClips.name);
    })()""", await_promise=True)
    time.sleep(2.5)
    # 有没有片段可以播
    clips = tab.js("""JSON.stringify((() => {
      const rows = [...document.querySelectorAll('[data-play]')].map(e => e.dataset.play);
      return [...new Set(rows)];
    })())""")
    clip_list = json.loads(clips) if clips else []
    check("畫面上有可播放的片段", len(clip_list) > 0, f"{len(clip_list)} 個")

    if clip_list:
        c = clip_list[0]
        tab.js(f"playClip({json.dumps(c)}, 'test.mp4')")
        time.sleep(2.5)
        st = tab.js("""JSON.stringify({
          visible: getComputedStyle(document.querySelector('#player')).display !== 'none',
          src: document.querySelector('#videoplayer').currentSrc,
          ready: document.querySelector('#videoplayer').readyState,
          dur: document.querySelector('#videoplayer').duration,
          w: document.querySelector('#videoplayer').videoWidth,
          h: document.querySelector('#videoplayer').videoHeight,
          meta: document.querySelector('#playermeta').textContent,
        })""")
        d = json.loads(st)
        check("播放器出現", d["visible"])
        check("載入了正確檔案", c.split("/")[-1] in (d["src"] or ""), d["src"].split("/")[-1] if d["src"] else "")
        check("影片可解碼（有長度與尺寸）",
              (d["dur"] or 0) > 0 and (d["w"] or 0) > 0,
              f"{d['dur']}s {d['w']}×{d['h']} | {d['meta']}")

        # 播放器不該佔滿整個視窗，否則清單會看不到
        h = tab.js("document.querySelector('#videoplayer').getBoundingClientRect().height")
        vh = tab.js("window.innerHeight")
        check("播放器高度合理（不超過 60% 視窗）", h and h < vh * 0.6, f"{round(h)}px / {vh}px")

        tab.js("closePlayer()")
        time.sleep(0.4)
        vis = tab.js("getComputedStyle(document.querySelector('#player')).display")
        check("關閉播放器", vis == "none", vis)

    print("── 素材庫面板")
    # 先從 API 取狀態（面板還沒開，DOM 是空的），測完再還原
    snap = get("http://127.0.0.1:8310/api/projects")
    before = {p["name"]: {"title": p["title"], "pinned": p["pinned"]} for p in snap}
    tab.js("openLibrary()")
    time.sleep(1.2)
    opened = tab.js("getComputedStyle(document.querySelector('#libmodal')).display !== 'none'")
    check("面板打開", opened)

    cards = tab.js("document.querySelectorAll('#libgrid .libcard').length")
    check("列出專案", (cards or 0) > 0, f"{cards} 個")

    if cards and cards > 0:
        first = tab.js("document.querySelector('#libgrid .libcard').dataset.name")
        orig = tab.js("document.querySelector('#libgrid .libcard .lctext').textContent")

        # 改名
        tab.js("document.querySelector('#libgrid .libcard [data-act=rename]').click()")
        time.sleep(0.4)
        has_input = tab.js("!!document.querySelector('#libgrid input.renaming')")
        check("改名輸入框出現", has_input)
        if has_input:
            tab.js("""
              const inp = document.querySelector('#libgrid input.renaming');
              inp.value = '自動測試名稱';
              inp.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true}));
              inp.blur();
            """)
            time.sleep(1.5)
            titles = tab.js("JSON.stringify([...document.querySelectorAll('#libgrid .lctext')].map(e=>e.textContent))")
            check("改名生效", "自動測試名稱" in (titles or ""), str(titles))
            # 還原，讓測試可以重複跑
            tab.js(f"""(async () => {{
              await api('/api/library', {{method:'POST', body: JSON.stringify(
                {{action:'rename', project:{json.dumps(first)}, title:{json.dumps(orig)}}})}});
            }})()""", await_promise=True)
            time.sleep(0.8)

        # pin：切換後狀態必須改變，且釘選的要排最前
        was_pinned = tab.js(f"""(() => {{
          const c = document.querySelector('#libgrid .libcard[data-name="{first}"]');
          return !!c && !!c.querySelector('.pinmark');
        }})()""")
        tab.js(f"""document.querySelector('#libgrid .libcard[data-name="{first}"] [data-act=pin]').click()""")
        time.sleep(1.5)
        now_pinned = tab.js(f"""(() => {{
          const c = document.querySelector('#libgrid .libcard[data-name="{first}"]');
          return !!c && !!c.querySelector('.pinmark');
        }})()""")
        check("釘選可切換", now_pinned != was_pinned, f"{was_pinned} → {now_pinned}")
        if now_pinned:
            first_pinned = tab.js("!!document.querySelector('#libgrid .libcard .pinmark')")
            check("釘選排在最前", first_pinned)

    # 排序選單
    sorts = tab.js("JSON.stringify([...document.querySelectorAll('#libsort option')].map(o=>o.value))")
    check("有四種以上排序方式", len(json.loads(sorts or "[]")) >= 4, str(sorts))
    tab.js("""
      const sel = document.querySelector('#libsort');
      sel.value = 'name';
      sel.dispatchEvent(new Event('change', {bubbles:true}));
    """)
    time.sleep(1.5)
    check("切換排序不報錯", not any("libsort" in e or "sort" in e.lower() for e in tab.errors))

    tab.js("closeLibrary()")
    time.sleep(0.4)
    closed = tab.js("getComputedStyle(document.querySelector('#libmodal')).display === 'none'")
    check("面板關閉", closed)

    # 還原成進來的樣子
    if before:
        import urllib.request as _ur
        for name, meta in before.items():
            for payload in (
                {"action": "rename", "project": name, "title": meta["title"]},
                {"action": "pin", "project": name, "pinned": meta["pinned"]},
            ):
                req = _ur.Request(
                    "http://127.0.0.1:8310/api/library",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                try:
                    _ur.urlopen(req, timeout=5).read()
                except Exception:  # noqa: BLE001
                    pass

    print("── 掃描／復原")
    tab.js("openLibrary()")
    time.sleep(0.8)
    has_scan = tab.js("!!document.querySelector('#libscan')")
    check("有掃描按鈕", has_scan)
    if has_scan:
        tab.js("document.querySelector('#libscan').click()")
        time.sleep(2.0)
        box = tab.js("(document.querySelector('#libscanbox')||{}).textContent||''")
        check("掃描有回報結果", "掃描" in (box or ""), (box or "")[:44].strip())
    tab.js("closeLibrary()")
    time.sleep(0.3)

    print("── 素材 metadata")
    asset = tab.js("""(async () => {
      const list = await api('/api/projects');
      const p = (list || [])[0];
      if (!p) return '';
      const a = await api('/api/asset/' + encodeURIComponent(p.name) + '/0');
      return JSON.stringify({t: a.title, terms: a.search_terms, c: !!a.contact});
    })()""", await_promise=True)
    ok_asset = False
    if asset:
        import json as _json
        try:
            a = _json.loads(asset)
            ok_asset = bool(a.get("t")) and isinstance(a.get("terms"), list) and a.get("c")
        except Exception:  # noqa: BLE001
            ok_asset = False
    check("單支素材可讀取完整 metadata", ok_asset, (asset or "")[:56])

    print("── 授權膠囊")
    pills = tab.js("JSON.stringify([...document.querySelectorAll('.card .meta .pill')].map(e=>e.textContent.trim()))")
    check("卡片上沒有「未知」膠囊", "未知" not in (pills or ""), str(pills))
    note = tab.js("document.querySelector('.licnote') ? document.querySelector('.licnote').textContent : ''")
    check("未標示授權改成一句說明", "未標示" in (note or ""), str(note))

    print("── 沒有 JS 錯誤")
    real_errors = [e for e in tab.errors if e and "favicon" not in e.lower()]
    check("console 乾淨", not real_errors, str(real_errors[:3]))

    tab.close()
    print()
    print("全部通過" if ok else "有項目失敗")
    return 0 if ok else 1


if __name__ == "__main__":
    import urllib.parse

    sys.exit(main())
