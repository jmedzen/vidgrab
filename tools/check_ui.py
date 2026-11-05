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
from pathlib import Path

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
                    # 帶上 URL，否則只看到「404」卻不知道是哪個請求
                    where = e.get("url") or ""
                    text = e.get("text", "")
                    self.errors.append(f"{text} @ {where}" if where else text)
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


def _start_fixture_server():
    """用假的素材庫起一個獨立實例（port 8310）。

    這樣介面檢查不會依賴、也不會弄壞真人正在用的 projects/。
    """
    import subprocess

    root = Path(__file__).resolve().parents[1]
    data = Path(tempfile.mkdtemp(prefix="vg-ui-"))
    make = root / "tools" / "make_fixture.py"
    subprocess.run([sys.executable, str(make), str(data)],
                   capture_output=True, timeout=120, check=False)

    env = {**os.environ,
           "VIDGRAB_HOME": str(data / "projects"),
           "VIDGRAB_AUTH_DIR": str(data / "auth"),
           "VIDGRAB_CONFIG": str(data / "config.json")}
    (data / "auth").mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [sys.executable, "-m", "vidgrab.cli", "web", "--no-open", "--port", "8310"],
        cwd=str(root), env={**env, "PYTHONPATH": str(root / "src")},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):  # 最多等 12 秒
        try:
            urllib.request.urlopen("http://127.0.0.1:8310/api/version", timeout=1).read()
            print(f"… 測試實例已啟動（素材在 {data}）")
            return proc
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    proc.terminate()
    return None


def main() -> int:
    import urllib.parse as up

    argv = sys.argv[1:]
    launch = "--launch" in argv
    headful = "--headful" in argv

    # --fixture：自己起一個「有測試素材的乾淨實例」，不動真實資料
    fixture_proc = None
    if "--fixture" in argv:
        fixture_proc = _start_fixture_server()
        if not fixture_proc:
            print("✗ 起不來測試實例")
            return 2

    # 先確認 Web UI 有在跑，否則後面的失敗訊息會很誤導
    try:
        urllib.request.urlopen("http://127.0.0.1:8310/api/version", timeout=3).read()
    except Exception:  # noqa: BLE001
        if fixture_proc:
            fixture_proc.terminate()
        print("✗ 找不到正在跑的 vidgrab Web UI（http://127.0.0.1:8310）")
        print("  請先在另一個終端機執行：./vidgrab web")
        print("  或者加 --fixture 讓它自己起一個有測試素材的實例")
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
        if fixture_proc:
            fixture_proc.terminate()
            try:
                fixture_proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
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

    # log 視窗要跟著主題：底色曾經寫死 rgba(8,10,15,.96)，
    # 淺色主題下變成深灰字印在黑底上（對比只有 3.5:1）
    console_bgs = {}
    for tid in ids:
        tab.js(f"applyTheme('{tid}')")
        tab.js("consoleLine('主題檢查')")
        time.sleep(0.15)
        info = tab.js("""JSON.stringify((() => {
          const c = document.querySelector('#console');
          const ln = c.querySelector('.ln');
          return {bg: getComputedStyle(c).backgroundColor,
                  fg: ln ? getComputedStyle(ln).color : ''};
        })())""")
        console_bgs[tid] = json.loads(info) if info else {}

    distinct = {v.get("bg") for v in console_bgs.values()}
    check("log 視窗底色跟著主題變", len(distinct) >= 5,
          f"{len(distinct)} 種底色")

    def _ratio(fg: str, bg: str) -> float:
        import re as _re

        def _rgb(t: str):
            n = [int(x) for x in _re.findall(r"\d+", t)[:3]]
            return n if len(n) == 3 else [0, 0, 0]

        def _lin(c: float) -> float:
            c /= 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        def _L(t):
            r, g, b = _rgb(t)
            return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)

        la, lb = _L(fg), _L(bg)
        hi, lo = max(la, lb), min(la, lb)
        return (hi + 0.05) / (lo + 0.05)

    worst = min(
        ((t, _ratio(v["fg"], v["bg"])) for t, v in console_bgs.items() if v.get("fg")),
        key=lambda x: x[1], default=("", 0.0),
    )
    check("log 文字對比 ≥ WCAG AA (4.5:1)", worst[1] >= 4.5,
          f"最低 {worst[0]} {worst[1]:.2f}:1")

    # 選單尺寸：曾經被 `aside * {max-width:100%}` 壓成 32px 的一條縫
    tab.js("$('#themebtn').click()")
    time.sleep(0.4)
    menu = tab.js("""JSON.stringify((() => {
      const m = document.querySelector('#thememenu');
      const r = m.getBoundingClientRect();
      const btns = [...m.querySelectorAll('button')].map(b => b.getBoundingClientRect());
      return {
        w: Math.round(r.width), h: Math.round(r.height),
        open: m.classList.contains('open'),
        n: btns.length,
        minBtnW: btns.length ? Math.round(Math.min(...btns.map(b => b.width))) : 0,
        labels: [...m.querySelectorAll('button')].map(b => b.textContent.trim()),
      };
    })())""")
    md = json.loads(menu) if menu else {}
    check("選單有打開", md.get("open") is True)
    check("選單寬度正常（不是被壓成一條縫）", (md.get("w") or 0) >= 180, f"{md.get('w')}px")
    check("選單列出六個主題", md.get("n") == 6, str(md.get("n")))
    check("每個選項都夠寬可點", (md.get("minBtnW") or 0) >= 150, f"最窄 {md.get('minBtnW')}px")
    # 選單不要超出視窗（右緣對齊是刻意的，但不該跑出去）
    overflow = tab.js("""(() => {
      const r = document.querySelector('#thememenu').getBoundingClientRect();
      return r.left >= 0 && r.right <= window.innerWidth + 1;
    })()""")
    check("選單沒有跑出視窗", overflow)

    # 每個選項的文字都要看得見：切到淺色主題時，深字配深底會隱形
    for tid in ("paper", "mist", "sakura"):
        tab.js(f"applyTheme('{tid}')")
        time.sleep(0.2)
        tab.js("$('#themebtn').click()")
        time.sleep(0.35)
        lum = tab.js("""(() => {
          // 選單是深色浮層；用實際背景與文字色算亮度差
          const m = document.querySelector('#thememenu');
          const b = [...m.querySelectorAll('button')].find(x => !x.getAttribute('aria-checked')
            || x.getAttribute('aria-checked') === 'false');
          if (!b) return -1;
          const cs = getComputedStyle(b);
          const ctx = document.createElement('canvas').getContext('2d');
          const toRGB = (c) => {
            ctx.fillStyle = c;
            const s = ctx.fillStyle;
            if (s.startsWith('#')) {
              return [parseInt(s.slice(1,3),16), parseInt(s.slice(3,5),16), parseInt(s.slice(5,7),16)];
            }
            return (s.match(/[0-9]+/g) || []).slice(0, 3).map(Number);
          };
          const rel = (rgb) => {
            const [r,g,bl] = rgb.map(v => {
              v /= 255;
              return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4);
            });
            return 0.2126*r + 0.7152*g + 0.0722*bl;
          };
          const fg = rel(toRGB(cs.color));
          const bg = rel([11,13,18]);  // 選單底色（深色浮層）
          const hi = Math.max(fg,bg), lo = Math.min(fg,bg);
          return (hi + 0.05) / (lo + 0.05);
        })()""")
        check(f"{tid} 主題下選單文字看得見", (lum or 0) >= 4.5, f"對比 {round(lum,2) if lum else lum}:1")
        tab.js("$('#themebtn').click()")
        time.sleep(0.2)

    tab.js("applyTheme('midnight')")
    time.sleep(0.2)
    tab.js("$('#themebtn').click()")
    time.sleep(0.35)

    # 分深淺兩組，且標籤看得懂
    groups = tab.js("JSON.stringify([...document.querySelectorAll('#thememenu .group')].map(e=>e.textContent.trim()))")
    check("分成深色／淺色兩組", len(json.loads(groups or "[]")) == 2, str(groups))

    # 點選項要真的換主題並關閉選單
    tab.js("""(() => {
      const b = [...document.querySelectorAll('#thememenu button')].find(x => x.dataset.theme === 'sakura');
      if (b) b.click();
    })()""")
    time.sleep(0.4)
    picked = tab.js("document.documentElement.dataset.theme")
    closed = tab.js("!document.querySelector('#thememenu').classList.contains('open')")
    check("點選項會換主題", picked == "sakura", str(picked))
    check("選完自動關閉選單", closed)

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
        # 等真的解出 metadata，不要用固定秒數賭 —— 大檔案偶爾會超過 2.5 秒，
        # 那時讀到的 duration 是 NaN，會被誤判成「影片壞了」。
        tab.js("""(() => new Promise(done => {
          const v = document.querySelector('#videoplayer');
          if (v.readyState >= 1) return done();
          const fin = () => done();
          v.addEventListener('loadedmetadata', fin, {once:true});
          v.addEventListener('error', fin, {once:true});
          setTimeout(fin, 8000);
        }))()""", await_promise=True)
        time.sleep(0.3)
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

        # ── 點外面／Esc 要關得掉（用真的事件測，不是直接呼叫 closePlayer）
        #
        # 注意：playClip 有 toggle 行為——再點同一個片段會「收起」。
        # 所以每次都要先確定是關著的再點開，並且**確認真的開了**，
        # 否則會變成在測「本來就關著」的假測試（踩過）。
        def _is_open():
            return tab.js(
                "getComputedStyle(document.querySelector('#player')).display"
            ) != "none"

        def _real_click(sel_or_xy):
            """用真的滑鼠事件點。

            播放器的「點外面關閉」聽的是 pointerdown，程式呼叫 .click()
            不會產生那個事件 —— 用合成的 click 測會得到假通過/假失敗。
            """
            if isinstance(sel_or_xy, str):
                box = tab.js("""(() => {
                  const el = document.querySelector(%s);
                  if (!el) return '';
                  el.scrollIntoView({block: 'center'});
                  const r = el.getBoundingClientRect();
                  return JSON.stringify({x: Math.round(r.x+r.width/2),
                                         y: Math.round(r.y+r.height/2)});
                })()""" % json.dumps(sel_or_xy))
                if not box:
                    return False
                xy = json.loads(box)
            else:
                xy = sel_or_xy
            for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
                tab.send("Input.dispatchMouseEvent", type=kind,
                         x=xy["x"], y=xy["y"], button="left", clickCount=1)
                time.sleep(0.12)
            return True

        def _open_player():
            if _is_open():
                tab.js("closePlayer()")
                time.sleep(0.3)
            if not _real_click('.clip-actions button[data-play]'):
                tab.js("document.querySelector('[data-play]').click()")
            time.sleep(1.4)
            return _is_open()

        assert _open_player(), "播放器沒開起來，後面的斷言就沒意義了"
        _real_click({"x": 60, "y": 120})   # 側欄空白處 = 播放器外面
        time.sleep(0.7)
        check("點播放器外面會關閉", not _is_open())

        assert _open_player(), "播放器沒開起來"
        _real_click('#player .ptitle')
        time.sleep(0.7)
        check("點播放器內部不會關閉", _is_open())

        assert _open_player(), "播放器沒開起來，Esc 那條會變成假測試"
        tab.send("Input.dispatchKeyEvent", type="keyDown", key="Escape",
                 code="Escape", windowsVirtualKeyCode=27)
        tab.send("Input.dispatchKeyEvent", type="keyUp", key="Escape",
                 code="Escape", windowsVirtualKeyCode=27)
        time.sleep(0.6)
        check("按 Esc 會關閉播放器", not _is_open())

    print("── 可及性")
    no_alt = tab.js(
        "Array.from(document.querySelectorAll('img'))"
        ".filter(i=>!i.hasAttribute('alt')).length"
    )
    check("圖片都有 alt", no_alt == 0, f"{no_alt} 張沒有")

    no_name = tab.js("""
      JSON.stringify(Array.from(document.querySelectorAll('button'))
        .filter(b => !(b.textContent||'').trim()
                     && !b.getAttribute('aria-label')
                     && !b.getAttribute('title'))
        .map(b => b.id || b.className))
    """)
    check("按鈕都有可讀名稱", json.loads(no_name or "[]") == [], no_name or "")

    unnamed = tab.js("""
      Array.from(document.querySelectorAll('input,select,textarea')).filter(el => {
        if (el.getAttribute('aria-label') || el.getAttribute('placeholder')) return false;
        if (el.id && document.querySelector(`label[for="${el.id}"]`)) return false;
        return !el.closest('label');
      }).length
    """)
    check("輸入元件都有標籤", (unnamed or 0) <= 3, f"{unnamed} 個沒有")

    live = tab.js("document.querySelectorAll('[aria-live]').length")
    check("有 aria-live 區域（進度會被唸出來）", (live or 0) >= 2, f"{live} 個")

    # 可點區域：真正的按鈕不該小到難點
    # （卡片標題那種容器式 <a> 不算操作目標，排除掉）
    small = tab.js("""
      Array.from(document.querySelectorAll('button, a.mini'))
        .filter(e => { const r = e.getBoundingClientRect();
                       return r.width > 0 && (r.height < 28 || r.width < 26); }).length
    """)
    check("按鈕不會小到難點", (small or 0) == 0, f"{small} 個偏小")

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

    print("── 系統設定（admin）")
    has_admin = tab.js("!!document.querySelector('#openadmin')")
    check("有設定入口", has_admin)
    if has_admin:
        tab.js("document.querySelector('#openadmin').click()")
        time.sleep(1.2)
        opened = tab.js("getComputedStyle(document.querySelector('#adminmodal')).display !== 'none'")
        check("設定面板打開", opened)

        n_inputs = tab.js("document.querySelectorAll('#adminmodal input,#adminmodal select').length")
        check("每個設定項都有輸入元件", (n_inputs or 0) >= 20, f"{n_inputs} 個")

        groups = tab.js("JSON.stringify([...document.querySelectorAll('#adminmodal h3')].map(e=>e.textContent))")
        gl = json.loads(groups or "[]")
        check("有分組（不是一長串）", len(gl) >= 3, str(gl))

        # 沒有「其他」孤兒 = 每個設定項都有人寫標籤
        check("沒有未分類的設定項", "其他" not in gl, str(gl))

        # 每個欄位都要有可讀名稱，不然讀屏軟體唸不出來
        unnamed = tab.js("""Array.from(document.querySelectorAll('#adminmodal input,#adminmodal select'))
          .filter(el => !(el.getAttribute('aria-label') || (el.id && document.querySelector(`label[for="${el.id}"]`)) || el.closest('label'))).length""")
        check("設定項都有標籤", (unnamed or 0) == 0, f"{unnamed} 個沒有")

        # 存檔流程：改一個值 → 儲存 → API 要真的變
        tab.js("""(() => {
          const el = document.querySelector('#cfg-per_keyword');
          const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
          set.call(el, '17');
          el.dispatchEvent(new Event('input', {bubbles:true}));
        })()""")
        time.sleep(0.4)
        check("改了值之後儲存鈕可用",
              tab.js("!document.querySelector('#adminsave').disabled"))
        tab.js("document.querySelector('#adminsave').click()")
        time.sleep(1.8)
        saved = tab.js("(async () => { const r = await api('/api/config'); return String(r.values.per_keyword); })()",
                       await_promise=True)
        check("儲存有寫進設定", saved == "17", f"per_keyword={saved}")

        # 還原
        tab.js("document.querySelector('#adminreset').click()")
        time.sleep(1.6)
        back = tab.js("(async () => { const r = await api('/api/config'); return String(r.values.per_keyword); })()",
                      await_promise=True)
        check("一鍵還原有效", back == "12", f"per_keyword={back}")

        tab.js("document.querySelector('#adminclose').click()")
        time.sleep(0.6)
        check("設定面板關閉",
              tab.js("getComputedStyle(document.querySelector('#adminmodal')).display === 'none'"))

    print("── ▶ 播放（真滑鼠點擊）")
    # 先確定播放器是關的
    tab.js("document.querySelector('#playerclose') && document.querySelector('#playerclose').click()")
    time.sleep(0.8)
    play_btn = tab.js("""(() => {
      const b = document.querySelector('.clip-actions button[data-play]');
      if (!b) return '';
      b.scrollIntoView({block: 'center'});
      const r = b.getBoundingClientRect();
      return JSON.stringify({x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2),
                             inView: r.top>=0 && r.bottom<=innerHeight});
    })()""")
    check("有 ▶ 播放按鈕", bool(play_btn), str(play_btn)[:40])
    if play_btn:
        ppos = json.loads(play_btn)
        check("播放鈕在可視範圍內", ppos.get("inView") is True)
        # 用 __CDP__ 派的真滑鼠事件：程式呼叫 .click() 測不出
        # 「剛註冊的 click 監聽把這次點擊當成點外面」這個競態。
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            tab.send("Input.dispatchMouseEvent", type=kind, x=ppos["x"], y=ppos["y"],
                     button="left", clickCount=1)
            time.sleep(0.12)
        time.sleep(2.2)
        check("點 ▶ 播放會打開播放器",
              tab.js("(() => { const p=document.querySelector('#player');"
                     " return !!p && getComputedStyle(p).display !== 'none'; })()"))
        pv = tab.js("""(async () => {
          const v = document.querySelector('#videoplayer');
          if (!v) return 'no-video';
          if (v.readyState < 2) {
            await new Promise(r => {
              v.addEventListener('loadedmetadata', r, {once:true});
              v.addEventListener('error', r, {once:true});
              setTimeout(r, 9000);
            });
          }
          return JSON.stringify({rs: v.readyState,
            dur: Math.round(v.duration*100)/100, muted: v.muted});
        })()""", await_promise=True)
        try:
            pvi = json.loads(pv)
            check("播放器影片載入成功", pvi["rs"] >= 2 and pvi["dur"] > 0, pv[:60])
            check("播放器不預設靜音", pvi.get("muted") is False, f"muted={pvi.get('muted')}")
        except Exception:  # noqa: BLE001
            check("播放器影片載入成功", False, str(pv)[:60])
        tab.js("document.querySelector('#playerclose') && document.querySelector('#playerclose').click()")
        time.sleep(0.6)

    print("── 大圖／影片預覽")
    thumb = tab.js("!!document.querySelector('.clip-thumb')")
    check("片段有縮圖可點", thumb)
    if thumb:
        tab.js("document.querySelector('.clip-thumb').click()")
        time.sleep(1.8)
        check("放大預覽打開",
              tab.js("document.querySelector('#lightbox').classList.contains('show')"))

        # 使用者要的是「放大看影片」，不是放大看圖
        check("放大後是影片（不是靜態圖）",
              tab.js("!!document.querySelector('#lightbox video')"))

        played = tab.js("""(async () => {
          const v = document.querySelector('#lightbox video');
          if (!v) return 'no-video';
          if (v.readyState < 2) {
            await new Promise(r => {
              v.addEventListener('loadedmetadata', r, {once:true});
              v.addEventListener('error', r, {once:true});
              setTimeout(r, 9000);
            });
          }
          if (v.error) return 'error:' + v.error.code;
          const t0 = v.currentTime;
          await new Promise(r => setTimeout(r, 1100));
          return JSON.stringify({
            rs: v.readyState, dur: Math.round(v.duration*100)/100,
            w: v.videoWidth, h: v.videoHeight,
            advancing: v.currentTime > t0,
            muted: v.muted,
          });
        })()""", await_promise=True)
        ok_play = False
        try:
            info = json.loads(played)
            ok_play = info["rs"] >= 2 and info["dur"] > 0 and info["advancing"]
        except Exception:  # noqa: BLE001
            info = played
        check("放大後影片真的在播", ok_play, str(played)[:70])
        try:
            check("放大預覽不預設靜音", info.get("muted") is False,
                  f"muted={info.get('muted')}")
        except Exception:  # noqa: BLE001
            check("放大預覽不預設靜音", False, str(played)[:60])

        # 點影片本身不該關掉（才拉得到進度條）
        before = tab.js("document.querySelector('#lightbox').classList.contains('show')")
        tab.js("(() => { const v=document.querySelector('#lightbox video'); if(v) v.click(); })()")
        time.sleep(0.5)
        check("點影片不會關掉預覽",
              tab.js("document.querySelector('#lightbox').classList.contains('show')") == before)

        # 點背景關閉（用真的滑鼠事件，JS .click() 會被 stopPropagation 繞過）
        box = tab.js("""(() => { const b=document.querySelector('#lightbox').getBoundingClientRect();
          return JSON.stringify({x: Math.round(b.x+8), y: Math.round(b.y+8)}); })()""")
        pos = json.loads(box)
        for kind in ("mousePressed", "mouseReleased"):
            tab.send("Input.dispatchMouseEvent", type=kind, x=pos["x"], y=pos["y"],
                     button="left", clickCount=1)
        time.sleep(0.8)
        check("點背景關掉預覽",
              tab.js("!document.querySelector('#lightbox').classList.contains('show')"))

        # Esc 也要能關
        tab.js("document.querySelector('.clip-thumb').click()")
        time.sleep(1.0)
        tab.send("Input.dispatchKeyEvent", type="keyDown", key="Escape", windowsVirtualKeyCode=27)
        time.sleep(0.6)
        check("Esc 關掉預覽",
              tab.js("!document.querySelector('#lightbox').classList.contains('show')"))

    # admin 面板不可以被包進 lightbox（會蓋住整個預覽區）
    check("設定面板不在預覽容器裡",
          not tab.js("!!document.querySelector('#lightbox #adminmodal')"))

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
