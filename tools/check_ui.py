#!/usr/bin/env python3
"""用 CDP 在真的 Chrome 裡驗證介面：主題、側欄寬度、播放器、JS 錯誤。

比截圖可靠（截圖在 headless 常常逾時），而且能直接斷言。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

import websocket

CDP = "http://127.0.0.1:9222"


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
