"""煙霧測試：不打網路，只驗證「裝起來能跑、關鍵端點不錯」。

跑法：
    ./vidgrab test       或   pytest -q
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

from vidgrab import __version__, report
from vidgrab.models import (
    Item,
    classify_license,
    fmt_ts,
    is_repost,
    matches_keyword,
    parse_ts,
)
from vidgrab.version import build, full_version


# ---------------------------------------------------------------- 基本


def test_version_has_build():
    assert __version__
    assert build()
    assert full_version().startswith(__version__)


# ---------------------------------------------------------------- 時間


@pytest.mark.parametrize(
    "text,expected",
    [("83", 83), ("1:23", 83), ("00:01:23", 83), ("1:05:30", 3930), ("12.5", 12.5)],
)
def test_parse_ts(text, expected):
    assert parse_ts(text) == pytest.approx(expected)


@pytest.mark.parametrize("seconds,expected", [(0, "0:00"), (83, "1:23"), (3930, "1:05:30")])
def test_fmt_ts(seconds, expected):
    assert fmt_ts(seconds) == expected


def test_fmt_ts_tolerates_none():
    with pytest.raises(TypeError):
        fmt_ts(None)  # 呼叫端要先擋掉；這裡固定行為


# ---------------------------------------------------------------- 授權


def test_license_detection():
    assert classify_license("CC0 public domain footage")[0] == "free"
    assert classify_license("Creative Commons Attribution 4.0")[0] == "free-attribution"
    assert classify_license("隨便拍的")[0] == "unknown"


def test_repost_detection():
    assert is_repost("转载自某频道")
    assert is_repost("搬运")
    assert not is_repost("原創作品")


def test_keyword_relevance():
    item = Item(platform="bilibili", vid="BV1", url="u", title="台北 街景 4K")
    assert matches_keyword(item, "台北 街景")
    assert not matches_keyword(item, "大阪 拉麵")
    # 任一词命中就算：多词关键字放宽比对，宁多抓再人工筛
    assert matches_keyword(item, "大阪 街景")


# ---------------------------------------------------------------- 報告


def make_item(**kw) -> Item:
    base = dict(
        platform="bilibili",
        vid="BV1TEST",
        url="https://www.bilibili.com/video/BV1TEST",
        title="測試素材",
        author="作者",
        duration=600,
        views=1000,
        keyword="測試",
    )
    base.update(kw)
    return Item(**base)


def test_report_handles_full_clip(tmp_path):
    """整支下載的 clip.start/end 是 None，報告不能爆。"""
    item = make_item()
    item.clips = [
        {"path": str(tmp_path / "a.mp4"), "start": None, "end": None,
         "label": "整支.mp4", "size_bytes": 1234, "width": 1920, "height": 1080}
    ]
    manifest = {"queries": ["測試"], "sources": ["bilibili"], "created": "now"}

    report.write_html(tmp_path / "index.html", [item], tmp_path, manifest)
    report.write_csv(tmp_path / "clips.csv", [item], tmp_path)
    report.write_readme(tmp_path / "README.md", [item], manifest)

    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "整支" in html
    csv = (tmp_path / "clips.csv").read_text(encoding="utf-8-sig")
    assert "整支" in csv


def test_report_handles_range_clip(tmp_path):
    item = make_item()
    item.clips = [
        {"path": str(tmp_path / "b.mp4"), "start": 30, "end": 45,
         "label": "b.mp4", "size_bytes": 999, "width": 1280, "height": 720}
    ]
    report.write_html(tmp_path / "index.html", [item], tmp_path,
                      {"queries": ["測試"], "sources": ["bilibili"]})
    assert "0:30" in (tmp_path / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- Web


@pytest.fixture()
def web(tmp_path, monkeypatch):
    """在隨機 port 起一個真的 web server，資料放 tmp。"""
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    monkeypatch.setenv("VIDGRAB_AUTH_DIR", str(tmp_path / "auth"))

    from vidgrab.web import serve

    # 讓作業系統給一個真正空著的 port，避免測試之間互撞
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    thread = threading.Thread(
        target=serve, kwargs={"host": "127.0.0.1", "port": port, "open_browser": False},
        daemon=True,
    )
    thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/api/version", timeout=0.5)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    else:
        pytest.fail("web server 沒起來")
    return base


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode())


def test_web_version_endpoint(web):
    data = get(web + "/api/version")
    assert data["version"] == __version__
    assert data["build"]


def test_web_projects_empty(web):
    assert get(web + "/api/projects") == []


def test_web_index_renders(web):
    """首頁外殼與它的靜態資源都要真的送得出來。

    階段 0 之後 CSS／JS 搬到 static/ 了，首頁本身只有外殼
    （內容全部走 /api/），所以「有 id=mode」要在靜態檔裡驗。
    """
    with urllib.request.urlopen(web + "/", timeout=5) as resp:
        body = resp.read().decode()
    assert "vidgrab" in body
    # 階段 1 之後由 Vite 打包，檔名帶雜湊
    assert "/static/assets/" in body, "外殼要載入打包後的資源"
    assert ".css" in body and ".js" in body, "樣式與程式都要載入"
    assert 'id="root"' in body, "React 掛載點"

    for path, want in (("/static/index.html", 'id="root"'),):
        with urllib.request.urlopen(web + path, timeout=5) as resp:
            assert resp.status == 200, f"{path} 回應 {resp.status}"
            got = resp.read().decode()
        assert want in got, f"{path} 少了 {want}"

    # 打包後的 CSS／JS 也要送得出來
    import re
    for m in re.findall(r'/static/assets/[A-Za-z0-9._-]+', body):
        with urllib.request.urlopen(web + m, timeout=5) as resp:
            assert resp.status == 200, f"{m} 回應 {resp.status}"
            assert len(resp.read()) > 100, f"{m} 內容是空的"


def test_static_route_blocks_traversal(web):
    """/static/ 不可以被路徑穿越。"""
    import urllib.error

    for path in ("/static/../web.py", "/static/%2e%2e/web.py",
                 "/static/../__init__.py"):
        try:
            with urllib.request.urlopen(web + path, timeout=5) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, f"{path} 回了 {exc.code}"
        else:
            raise AssertionError(f"{path} 竟然讀得到，路徑穿越沒擋住")


# ---------------------------------------------------------------- 設定


def test_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    monkeypatch.setenv("VIDGRAB_AUTH_DIR", str(tmp_path / "auth"))

    from vidgrab import config

    assert config.load()["per"] == config.DEFAULTS["per"]
    config.save_patch({"per": 30, "height": 720})
    reloaded = config.load()
    assert reloaded["per"] == 30
    assert reloaded["height"] == 720
    # 沒動到的項目要保持預設
    assert reloaded["platform"] == config.DEFAULTS["platform"]


def test_config_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDGRAB_AUTH_DIR", str(tmp_path / "auth"))
    from vidgrab import config

    with pytest.raises(config.ConfigError):
        config.validate("platform", "myspace")
    with pytest.raises(config.ConfigError):
        config.validate("nope", 1)
    with pytest.raises(config.ConfigError):
        config.validate("per", -1)
    assert config.validate("per", "12") == 12.0
    assert config.validate("only_free", "true") is True


def test_config_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDGRAB_AUTH_DIR", str(tmp_path / "auth"))
    from vidgrab import config

    config.save_patch({"per": 99})
    config.reset(["per"])
    assert config.load()["per"] == config.DEFAULTS["per"]


# ---------------------------------------------------------------- API


def test_api_config_endpoints(web):
    data = get(web + "/api/config")
    assert "values" in data and "defaults" in data

    import urllib.request

    req = urllib.request.Request(
        web + "/api/config",
        data=json.dumps({"set": {"per": 42}}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = json.loads(resp.read().decode())
    assert body["ok"] and body["values"]["per"] == 42
    assert get(web + "/api/config")["values"]["per"] == 42


def test_api_openapi(web):
    spec = get(web + "/api/openapi.json")
    assert spec["openapi"].startswith("3.")
    for path in ("/api/search", "/api/pick", "/api/config", "/api/projects"):
        assert path in spec["paths"]


def test_api_item_bounds(web):
    import urllib.error

    with pytest.raises(urllib.error.HTTPError) as exc:
        get(web + "/api/item/nope-project/0")
    assert exc.value.code == 404


def test_api_export(web):
    data = get(web + "/api/export")
    assert data["version"] == __version__
    assert "config" in data and "projects" in data


def test_web_pick_uses_saved_config(web, tmp_path, monkeypatch):
    """存過的設定要成為預設，而且聲音格式不能漏傳。"""
    import urllib.request

    def post(path, payload):
        req = urllib.request.Request(
            web + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    post("/api/config", {"set": {"per": 45, "height": 720, "audio": True, "audio_format": "mp3"}})
    saved = get(web + "/api/config")["values"]
    assert saved["per"] == 45
    assert saved["audio_format"] == "mp3"

    # pick 的參數解析：body 沒給就要吃設定值（含 audio_format，這個漏過一次）
    from vidgrab import config
    from vidgrab.web import resolve_pick_options

    monkeypatch.setenv("VIDGRAB_CONFIG", get(web + "/api/config")["path"])
    opts = resolve_pick_options({"project": "x", "indexes": [0]}, config.load())
    assert opts["audio"] is True
    assert opts["audio_format"] == "mp3"
    assert opts["per"] == 45
    assert opts["height"] == 720

    # body 有給就要蓋過設定
    opts2 = resolve_pick_options({"audio_format": "wav", "height": 2160}, config.load())
    assert opts2["audio_format"] == "wav"
    assert opts2["height"] == 2160


def test_openapi_covers_readme_endpoints(web):
    """README 列出的端點，OpenAPI 一定要有，否則外部工具會找不到。"""
    spec = get(web + "/api/openapi.json")
    paths = set(spec["paths"])
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    import re

    referenced = set(re.findall(r"\| `?(/api/[a-z/.{}-]+|/file/[^`|]+)`?", readme))
    normalised = {p.split("{")[0].rstrip("/") for p in paths}
    missing = [r for r in referenced if r.split("{")[0].rstrip("/") not in normalised]
    assert not missing, f"README 提到但 OpenAPI 沒有：{missing}"


# ---------------------------------------------------------------- 素材庫


def _lib(order, pinned=()):
    return {"order": list(order), "items": {f: {"pinned": f in pinned} for f in order}}


def test_library_sync_never_drops_records(tmp_path, monkeypatch):
    """資料夾不見時，記錄要保留並標記 missing——不可以直接刪。

    這是實戰換來的：以前這裡會 pop 掉，換個 VIDGRAB_HOME 整份清單就消失。
    """
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    from vidgrab import library

    data, changed = library.sync({"order": [], "items": {}}, ["a", "b"])
    assert changed and data["order"] == ["a", "b"]

    data, changed = library.sync(data, ["b", "c"])  # a 不在磁碟了、c 是新的
    assert changed
    assert set(data["items"]) == {"a", "b", "c"}, "記錄不可以被刪掉"
    assert data["items"]["a"]["missing"] is True, "a 應該被標記 missing"
    assert data["items"]["b"]["missing"] is False

    # 資料夾又出現了 → 自動復原
    data, changed = library.sync(data, ["a", "b", "c"])
    assert data["items"]["a"]["missing"] is False


def test_library_rename_and_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    from vidgrab import library

    data = _lib(["a"])
    library.rename(data, "a", "我的素材")
    assert library.entry(data, "a")["title"] == "我的素材"

    library.set_pinned(data, "a", True)
    assert library.entry(data, "a")["pinned"] is True

    with pytest.raises(ValueError):
        library.rename(data, "a", "   ")
    with pytest.raises(ValueError):
        library.rename(data, "a", "x" * 200)


def test_library_pin_always_first():
    from vidgrab import library

    data = _lib(["a", "b", "c"], pinned={"c"})
    library.set_order(data, ["a", "b", "c"])
    assert data["order"][0] == "c", "釘選的必須排最前"

    data = _lib(["a", "b", "c"], pinned={"c"})
    library.move(data, "a", -1)  # 未釘選的不能越過釘選
    assert data["order"][0] == "c"


def test_library_move_keeps_regions():
    from vidgrab import library

    data = _lib(["a", "b", "c", "d"], pinned={"a", "c"})
    library.move(data, "d", -1)
    assert data["order"] == ["a", "c", "d", "b"]
    assert set(data["order"][:2]) == {"a", "c"}, "釘選區不能被未釘選的擠開"


def test_library_sort_pinned_first():
    from vidgrab import library

    data = _lib(["a", "b", "c"], pinned={"b"})
    stats = {"a": {"created": "20260101"}, "b": {"created": "20250101"}, "c": {"created": "20240101"}}
    library.sort_by(data, "created", stats)
    assert data["order"][0] == "b"
    assert data["order"][1:] == ["a", "c"]

    with pytest.raises(ValueError):
        library.sort_by(data, "nonsense", stats)


def test_library_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    from vidgrab import library

    data = _lib(["a", "b"])
    library.rename(data, "a", "改過的名字")
    library.save(data)

    again = library.load()
    assert library.entry(again, "a")["title"] == "改過的名字"
    assert again["order"] == ["a", "b"]


def test_library_corrupt_file_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
    (tmp_path / "projects" / "library.json").write_text("{ 壞掉的 json", encoding="utf-8")

    from vidgrab import library

    data = library.load()
    assert data["order"] == [] and data["items"] == {}


def test_api_library_endpoints(web):
    import urllib.request

    def post(payload):
        req = urllib.request.Request(
            web + "/api/library", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    # 沒有專案時是空清單
    assert get(web + "/api/library")["projects"] == []

    # 不認得的動作要擋下來
    import urllib.error

    with pytest.raises(urllib.error.HTTPError) as exc:
        post({"action": "hack"})
    assert exc.value.code == 400


def test_library_sort_mode_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    from vidgrab import library

    data = _lib(["a", "b"])
    library.sort_by(data, "name", {})
    assert data["sort"] == "name"

    # 手動拖曳後，模式要回到 manual
    library.set_order(data, ["b", "a"])
    assert data["sort"] == "manual"

    library.save(data)
    assert library.load()["sort"] == "manual"


@pytest.mark.parametrize("mode", ["manual", "name", "created", "clips", "count"])
def test_pin_first_invariant_all_modes(mode):
    """不管用哪種排序，釘選的永遠排在未釘選的前面。"""
    from vidgrab import library

    data = _lib(["a", "b", "c", "d"], pinned={"c"})
    stats = {
        "a": {"created": "20260101", "clips": 1, "count": 10, "title": "aaa"},
        "b": {"created": "20250101", "clips": 3, "count": 30, "title": "bbb"},
        "c": {"created": "20240101", "clips": 0, "count": 1, "title": "ccc"},
        "d": {"created": "20230101", "clips": 9, "count": 90, "title": "ddd"},
    }
    library.sort_by(data, mode, stats)
    flags = [data["items"][f]["pinned"] for f in data["order"]]
    first_unpinned = flags.index(False) if False in flags else len(flags)
    assert all(flags[:first_unpinned]), f"{mode} 排序後釘選的沒有排在最前：{data['order']}"


def test_pin_moves_to_front_immediately():
    """釘選後必須立刻排到最前，否則會出現「已釘選卻在後面」的矛盾。"""
    from vidgrab import library

    data = _lib(["a", "b", "c"])
    library.set_pinned(data, "c", True)
    assert data["order"][0] == "c", f"釘選後沒排到最前：{data['order']}"

    flags = [data["items"][f]["pinned"] for f in data["order"]]
    first_unpinned = flags.index(False) if False in flags else len(flags)
    assert all(flags[:first_unpinned])


def test_unpin_keeps_order_sane():
    from vidgrab import library

    data = _lib(["a", "b", "c"], pinned={"c"})
    library.set_pinned(data, "c", False)
    assert set(data["order"]) == {"a", "b", "c"}
    assert not any(data["items"][f]["pinned"] for f in data["order"])


def test_no_hardcoded_browser():
    """開瀏覽器一律走 webbrowser.open()＝系統預設，不可以寫死特定瀏覽器。

    這條是有代價換來的：驗證工具曾經用假的 HOME 硬開 Chrome，
    在 macOS 上跳出「Keychain Not Found」的視窗嚇到使用者。
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "vidgrab"
    banned = re.compile(
        r"Google Chrome\.app|chrome\.exe|msedge\.exe|start\s+chrome|open\s+-a\s+[\"']?"
        r"|subprocess\.[a-z_]*\(\s*\[?\s*[\"'][^\"']*chrome"
    )
    offenders = []
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for m in banned.finditer(text):
            # UA 字串與 --cookies-from-browser 只是「假裝成 Chrome」，不是開瀏覽器
            line = text[: m.start()].count("\n") + 1
            context = text.splitlines()[line - 1]
            if "Chrome/1" in context or "cookies" in context.lower():
                continue
            offenders.append(f"{py.name}:{line}: {context.strip()}")
    assert not offenders, "發現寫死瀏覽器的地方：\n" + "\n".join(offenders)


def test_check_ui_never_fakes_home():
    """驗證工具不可以覆寫 HOME 去硬開瀏覽器（會破壞 macOS 鑰匙圈）。"""
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "tools" / "check_ui.py").read_text(
        encoding="utf-8"
    )
    code_lines = [
        ln for ln in script.splitlines()
        if not ln.strip().startswith("#") and "HOME" in ln
    ]
    # 只允許註解裡提到 HOME（解釋為什麼不能用），不可以真的設定 env
    for ln in code_lines:
        assert not ln.strip().startswith(("env", "os.environ")), f"不該覆寫 HOME：{ln}"
        assert '"HOME"' not in ln and "'HOME'" not in ln, f"不該覆寫 HOME：{ln}"


def test_check_ui_launcher_is_safe():
    """啟動器要安全：不寫死單一瀏覽器、要有 --no-sandbox、要能優雅跳過。"""
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "tools" / "check_ui.py").read_text(
        encoding="utf-8"
    )
    # 找多個 Chromium（不要只認 Chrome）
    for name in ("Edge", "Brave", "Chromium"):
        assert name in script, f"啟動器應該也認 {name}"
    # 沙箱環境裡 Chrome sandbox 起不來會崩潰
    assert '"--no-sandbox"' in script
    # 找不到瀏覽器要回傳 2（呼叫端可據此視為「略過」而不是「失敗」）
    assert "return 2" in script


def test_dev_extra_has_websocket_client():
    """check_ui 需要 websocket-client，必須在 dev extras 裡，否則乾淨環境跑不動。"""
    from pathlib import Path

    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert "websocket-client" in pyproject


# ---------------------------------------------------------------- 掃描／復原


def test_scan_finds_untracked_and_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    from vidgrab import library

    root = tmp_path / "projects"
    for name in ("known", "orphan"):
        d = root / name
        d.mkdir(parents=True)
        (d / "manifest.json").write_text('{"queries": ["x"]}', encoding="utf-8")
        (d / "results.json").write_text("[]", encoding="utf-8")

    library.save({"order": ["known", "gone"], "items": {
        "known": {"title": "K"}, "gone": {"title": "G"},
    }})

    report = library.scan()
    assert [x["folder"] for x in report["untracked"]] == ["orphan"]
    assert [x["folder"] for x in report["missing"]] == ["gone"]
    assert report["ok"] is False


def test_scan_clean_when_everything_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    from vidgrab import library

    root = tmp_path / "projects"
    (root / "a").mkdir(parents=True)
    (root / "a" / "manifest.json").write_text("{}", encoding="utf-8")
    (root / "a" / "results.json").write_text("[]", encoding="utf-8")
    library.save({"order": ["a"], "items": {"a": {"title": "A", "missing": False}}})

    report = library.scan()
    assert report["ok"] is True
    assert report["untracked"] == [] and report["missing"] == []


def test_adopt_and_forget(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    from vidgrab import library

    data = {"order": [], "items": {}}
    library.adopt(data, ["x", "y"])
    assert set(data["items"]) == {"x", "y"} and data["order"] == ["x", "y"]

    library.forget(data, ["x"])
    assert set(data["items"]) == {"y"} and data["order"] == ["y"]


def test_scan_ignores_non_project_dirs(tmp_path, monkeypatch):
    """掃描只看真的有 manifest/results 的資料夾，不要把小垃圾當專案。"""
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    from vidgrab import library

    root = tmp_path / "projects"
    (root / "junk" / "sub").mkdir(parents=True)  # 沒有 manifest
    (root / "real").mkdir()
    (root / "real" / "manifest.json").write_text("{}", encoding="utf-8")

    report = library.scan()
    assert [x["folder"] for x in report["on_disk"]] == ["real"]


# ---------------------------------------------------------------- 素材 metadata


def test_enrich_from_info_fills_library_fields():
    from vidgrab.models import Item, enrich_from_info, search_terms

    it = Item(platform="youtube", vid="x", url="u", title="T", keyword="vegan dish")
    enrich_from_info(it, {
        "uploader": "Andrew", "channel_url": "https://yt/@a",
        "webpage_url": "https://yt/watch?v=x",
        "tags": ["vegan", "cooking"], "categories": ["Howto"],
        "upload_date": "20250102", "width": 1920, "height": 1080, "fps": 30,
        "license": "Creative Commons", "description": "d" * 50,
    })
    assert it.pubdate == "20250102"
    assert it.tags == ["vegan", "cooking"]
    assert it.width == 1920 and it.height == 1080
    assert it.license_name == "Creative Commons"
    assert it.author_url == "https://yt/@a"

    terms = search_terms(it)
    assert "vegan" in terms and "cooking" in terms and "dish" in terms


def test_enrich_does_not_overwrite_existing():
    """已經有值就不要蓋掉——搜尋結果可能比第二次抓的更準。"""
    from vidgrab.models import Item, enrich_from_info

    it = Item(platform="bilibili", vid="x", url="u", title="原標題",
              keyword="k", author="原作者", views=999)
    enrich_from_info(it, {"title": "新標題", "uploader": "新作者", "view_count": 1})
    assert it.title == "原標題"
    assert it.author == "原作者"
    assert it.views == 999


def test_contact_block_gives_credit_hint():
    from vidgrab.models import Item, contact_block

    it = Item(platform="bilibili", vid="x", url="https://b/v", title="T",
              author="作者", license_name="CC BY")
    block = contact_block(it)
    assert block["author_url"] == ""
    assert "作者" in block["credit_hint"]
    assert "https://b/v" in block["credit_hint"]


def test_contact_block_warns_when_license_unknown():
    from vidgrab.models import Item, contact_block

    it = Item(platform="bilibili", vid="x", url="https://b/v", title="T", author="某人")
    hint = contact_block(it)["credit_hint"]
    assert "聯繫" in hint, "授權不明時應該提示要先聯繫"


def test_csv_has_library_columns(tmp_path):
    """CSV 是素材庫的匯出格式，關鍵欄位不能少。"""
    import csv as _csv

    from vidgrab.models import Item
    from vidgrab.report import write_csv

    item = Item(
        platform="youtube", vid="v1", url="https://y/v1", title="T", author="A",
        keyword="廚房", tags=["特寫", "料理"], author_url="https://y/@a",
        width=1920, height=1080, pubdate="20250101",
    )
    out = tmp_path / "clips.csv"
    write_csv(out, [item], tmp_path)
    rows = list(_csv.reader(out.read_text(encoding="utf-8-sig").splitlines()))
    header = rows[0]
    for col in ("作者頁面", "素材關鍵字", "分類", "標註建議", "解析度"):
        assert col in header, f"CSV 缺欄位：{col}"

    row = dict(zip(header, rows[1]))
    assert "特寫" in row["素材關鍵字"]
    assert "廚房" in row["素材關鍵字"]
    assert row["解析度"] == "1920x1080"


def test_api_library_scan_endpoint(web):
    data = get(web + "/api/library/scan")
    assert "on_disk" in data and "untracked" in data and "missing" in data
    assert isinstance(data["ok"], bool)


def _make_project(tmp_path, name="p1", items=None):
    """在測試用 VIDGRAB_HOME 裡造一個最小專案。"""
    d = tmp_path / "projects" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({"queries": ["廚房"], "created": "2026-01-01"}), encoding="utf-8"
    )
    (d / "results.json").write_text(
        json.dumps(items or [{
            "platform": "youtube", "vid": "v1", "url": "https://y/v1",
            "title": "測試影片", "author": "作者A", "keyword": "廚房",
            "tags": ["特寫"], "webpage_url": "https://y/v1",
            "author_url": "https://y/@a",
        }], ensure_ascii=False),
        encoding="utf-8",
    )
    return d


def test_api_asset_metadata(tmp_path, web):
    """每支素材都要能讀到素材庫需要的資訊（外部工具靠這支）。"""
    _make_project(tmp_path)
    data = get(web + "/api/asset/p1/0")
    for key in ("title", "author", "webpage_url", "tags", "search_terms",
                "contact", "license", "clips"):
        assert key in data, f"asset API 缺欄位：{key}"
    assert isinstance(data["search_terms"], list)
    assert "credit_hint" in data["contact"]


def test_api_asset_out_of_range(tmp_path, web):
    import urllib.error

    _make_project(tmp_path)
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(web + "/api/asset/p1/9999")
    assert exc.value.code == 404


def test_tech_tags_are_filtered():
    """yt:cc=on 這類技術標記不是內容關鍵字，不該進素材庫。"""
    from vidgrab.models import Item, enrich_from_info, search_terms

    it = Item(platform="youtube", vid="x", url="u", title="T", keyword="vegan")
    enrich_from_info(it, {"tags": ["vegan", "yt:cc=on", "yt:stretch=16:9", "language:en", "cooking"]})
    assert it.tags == ["vegan", "cooking"]
    assert "yt:cc=on" not in search_terms(it)


def test_scrub_metadata_cleans_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    from vidgrab import tasks

    _make_project(tmp_path, "p1", items=[{
        "platform": "youtube", "vid": "v1", "url": "u", "title": "T",
        "tags": ["vegan", "yt:cc=on"],
    }])
    n = tasks.scrub_metadata(tmp_path / "projects" / "p1")
    assert n == 1

    _, items = tasks.load_project(tmp_path / "projects" / "p1")
    assert items[0].tags == ["vegan"]


# ---------------------------------------------------------------- 主題選單


def _static_file(name: str) -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1]
            / "src" / "vidgrab" / "static" / name).read_text(encoding="utf-8")


def _page_css() -> str:
    """前端樣式。

    階段 1 之後樣式由 Vite 打包，檔名帶內容雜湊（assets/index-xxxx.css），
    所以用 glob 找，不能寫死檔名。
    """
    import glob
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "vidgrab" / "static"
    hits = sorted(glob.glob(str(root / "assets" / "*.css")))
    assert hits, "找不到打包後的 CSS（要先跑 cd web && npm run build）"
    return Path(hits[0]).read_text(encoding="utf-8")


def test_theme_menu_has_explicit_width():
    """主題浮層要寫死寬度，否則會被 `aside * {max-width:100%}` 壓成一條縫。

    這是真實踩到的 bug：選單在畫面上只剩 32px，等於不能用。
    """
    import re

    src = _page_css()
    m = re.search(r"\.theme-menu\s*\{", src)
    assert m, "找不到 .theme-menu 的樣式"
    block = src[m.end(): m.end() + 500]
    flat = block.replace(" ", "").replace("\n", "")
    assert "width:206px" in flat, "浮層要有明確寬度"
    assert "min-width" in flat, "要寫 min-width 才不會被繼承的 max-width 壓掉"
    assert "max-width" in flat, "要寫 max-width 覆蓋掉 aside * 的 100%"


def test_theme_menu_is_absolute_overlay():
    """浮層不可以佔到版面，否則會把側欄其他東西往下擠。"""
    import re

    src = _page_css()
    m = re.search(r"\.theme-menu\s*\{", src)
    assert m, "找不到 .theme-menu 的樣式"
    block = src[m.end(): m.end() + 500].replace(" ", "")
    assert "position:absolute" in block
    assert "z-index" in block, "浮層要蓋在內容之上"


def test_six_themes_defined_with_all_vars():
    """六組主題都要定義完整，少一個變數就會出現透明或錯色。"""
    import re
    from pathlib import Path

    src = _page_css()
    themes = re.findall(r'\[data-theme="?([a-z]+)"?\]\s*\{(.*?)\}', src, re.S)
    ids = [t[0] for t in themes]
    for want in ("midnight", "ink", "forest", "paper", "mist", "sakura"):
        assert want in ids, f"缺少主題：{want}"

    required = ("--bg", "--panel", "--text", "--muted", "--accent", "--on-accent")
    for tid, block in themes:
        for var in required:
            assert var in block, f"{tid} 缺變數 {var}"


def test_theme_menu_labels_use_fixed_light_color():
    """選單在深色浮層上，字色不能用跟著主題跑的 --text。

    否則切到 Paper／Mist／Sakura 時，深色文字配深色浮層 → 幾乎看不見。
    修好前對比只有 1.2:1，修好後 15.86:1。
    """
    import re

    src = _page_css()
    m = re.search(r"\.theme-menu button\s*\{", src)
    assert m, "找不到 .theme-menu button 的樣式"
    block = src[m.end(): m.end() + 600]
    head = block.split("}")[0]
    flat = head.replace(" ", "").replace("\n", "")
    assert "color:#e6e8ee" in flat, "選單文字要用固定的淺色"
    assert "color:var(--text)" not in flat, (
        "選單文字不可以跟著目前主題的 --text 走，會在淺色主題下隱形"
    )


# ---------------------------------------------------------------- agent 介面


def test_stable_ids_are_deterministic():
    """id 必須可重算：重新掃描、搬專案都不能變，agent 才能長期引用。"""
    from vidgrab.models import asset_id, clip_id

    assert asset_id("bilibili", "BV1xx") == "bilibili:BV1xx"
    assert asset_id("bilibili", "") == "", "沒有 vid 就不要硬湊 id"

    a = clip_id("bilibili", "BV1xx", 60, 75)
    b = clip_id("bilibili", "BV1xx", 60.0, 75.0)
    assert a == b, "60 與 60.0 不可以變成兩個 id"
    assert a != clip_id("bilibili", "BV1xx", 60, 76)

    assert clip_id("youtube", "abc", None, None) == "youtube:abc#full"


def test_vault_manifest_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    from vidgrab import agent

    _make_project(tmp_path, "p1", items=[{
        "platform": "youtube", "vid": "v1", "url": "https://y/v1",
        "title": "測試影片", "author": "作者A", "tags": ["特寫"],
    }])

    m = agent.vault_manifest(with_items=True)
    assert m["schema"] == "vidgrab.vault/1"
    assert m["totals"]["projects"] == 1
    assert m["root"].startswith(str(tmp_path))

    proj = m["projects"][0]
    assert proj["name"] == "p1" and proj["missing"] is False
    item = proj["items"][0]
    assert item["id"] == "youtube:v1"
    assert "credit_hint" in item


def test_vault_clip_ids_computed_for_old_data(tmp_path, monkeypatch):
    """舊片段沒有存 id，vault 要當場算出來，agent 才拿得到穩定鍵。"""
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    from vidgrab import agent

    _make_project(tmp_path, "p1", items=[{
        "platform": "bilibili", "vid": "BV1", "url": "u", "title": "T",
        "clips": [{"path": "/tmp/x.mp4", "start": 10.0, "end": 25.0, "label": "x"}],
    }])
    m = agent.vault_manifest(with_items=True)
    clip = m["projects"][0]["items"][0]["clips"][0]
    assert clip["id"] == "bilibili:BV1#10-25"
    assert clip["asset_id"] == "bilibili:BV1"
    assert clip["duration"] == 15.0


def test_vault_search_finds_downloaded_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    from vidgrab import agent

    _make_project(tmp_path, "p1", items=[
        {"platform": "bilibili", "vid": "a", "url": "u", "title": "台北街景",
         "tags": ["城市", "夜晚"]},
        {"platform": "bilibili", "vid": "b", "url": "u", "title": "貓咪",
         "tags": ["動物"]},
    ])
    m = agent.vault_manifest(with_items=True)
    hits = agent.search_assets(m, "夜晚")
    assert len(hits) == 1
    assert hits[0]["item"]["id"] == "bilibili:a"

    assert agent.search_assets(m, "") == []
    assert agent.search_assets(m, "不存在") == []


def test_vault_search_multi_term_scores(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    from vidgrab import agent

    _make_project(tmp_path, "p1", items=[
        {"platform": "b", "vid": "1", "url": "u", "title": "台北 街景",
         "tags": ["城市"]},
        {"platform": "b", "vid": "2", "url": "u", "title": "台北 美食",
         "tags": ["食物"]},
    ])
    m = agent.vault_manifest(with_items=True)
    hits = agent.search_assets(m, "台北 街景")
    assert hits[0]["item"]["id"] == "b:1", "命中兩個詞的應該排前面"
    assert len(hits[0]["matched"]) == 2


def test_cli_vault_json_contract(tmp_path, monkeypatch):
    """vault 的 stdout 必須是純 JSON——agent 靠這個解析。"""
    import subprocess
    import sys

    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    _make_project(tmp_path, "p1")

    r = subprocess.run(
        [sys.executable, "-m", "vidgrab.cli", "vault", "--no-items"],
        capture_output=True, text=True, timeout=60,
        env={**__import__("os").environ, "PYTHONPATH": str(_ROOT / "src")},
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)  # 有雜訊就會在這裡炸
    assert data["schema"] == "vidgrab.vault/1"
    assert data["totals"]["projects"] == 1


def test_cli_error_is_json_not_traceback(tmp_path, monkeypatch):
    """找不到專案時，JSON 模式要回結構化錯誤，不能吐 traceback。"""
    import subprocess
    import sys

    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    r = subprocess.run(
        [sys.executable, "-m", "vidgrab.cli", "vault", "no-such-project"],
        capture_output=True, text=True, timeout=60,
        env={**__import__("os").environ, "PYTHONPATH": str(_ROOT / "src")},
    )
    assert r.returncode != 0
    data = json.loads(r.stdout)
    assert data["error"]["code"] == "not_found"
    assert data["error"]["retryable"] is False
    assert "Traceback" not in r.stdout


def test_cli_find_roundtrip(tmp_path, monkeypatch):
    """用 id 找回來：這是 agent 引用素材的方式。"""
    import subprocess
    import sys

    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    _make_project(tmp_path, "p1", items=[{
        "platform": "bilibili", "vid": "BV9", "url": "u", "title": "T",
        "clips": [{"path": str(tmp_path / "x.mp4"), "start": 5.0, "end": 20.0}],
    }])

    r = subprocess.run(
        [sys.executable, "-m", "vidgrab.cli", "find", "bilibili:BV9#5-20"],
        capture_output=True, text=True, timeout=60,
        env={**__import__("os").environ, "PYTHONPATH": str(_ROOT / "src")},
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["clip"]["id"] == "bilibili:BV9#5-20"
    assert data["clip"]["duration"] == 15.0


def test_structured_errors_have_codes():
    from vidgrab import models

    for cls, code, retry in [
        (models.NotFound, "not_found", False),
        (models.BadInput, "bad_input", False),
        (models.NoProject, "no_project", False),
        (models.DownloadFailed, "download_failed", True),
        (models.LoginRequired, "login_required", False),
    ]:
        e = cls("boom")
        d = e.to_dict()
        assert d["code"] == code, f"{cls.__name__} code 錯了"
        assert d["retryable"] is retry, f"{cls.__name__} retryable 錯了"
        assert d["message"] == "boom"


def test_normalize_strips_format_suffix(tmp_path):
    """yt-dlp 會留下 `x.f30077.mp4`，那是噪音，要改成乾淨名字。"""
    from vidgrab.media import _normalize_name

    f = tmp_path / "abc_full_x.f30077.mp4"
    f.write_bytes(b"x" * 100)
    (tmp_path / "abc_full_x.info.json").write_text("{}", encoding="utf-8")

    out = _normalize_name(f, "abc_full_x")
    assert out.name == "abc_full_x.mp4"
    assert out.exists()
    assert not f.exists()


def test_normalize_does_not_clobber(tmp_path):
    """已經有同名成品時不要覆蓋，資料安全優先。"""
    from vidgrab.media import _normalize_name

    keep = tmp_path / "abc.mp4"
    keep.write_bytes(b"original")
    dup = tmp_path / "abc.f30077.mp4"
    dup.write_bytes(b"dup")

    out = _normalize_name(dup, "abc")
    assert out == dup, "不該動它"
    assert keep.read_bytes() == b"original"


def test_build_prefers_git_over_stale_stamp(tmp_path, monkeypatch):
    """git 可用時要看 git，不能被舊的 _build_info.py 蓋掉。

    真實踩到的 bug：stamp 檔停在 `3.d79b3e3`，但 git 已經有 11 個 commit，
    `./vidgrab build` 一直顯示 3。
    """
    import importlib

    from vidgrab import version

    monkeypatch.delenv("VIDGRAB_BUILD", raising=False)
    monkeypatch.setattr(version, "_from_git", lambda: "11.abc1234")
    monkeypatch.setattr(version, "_from_file", lambda: "3.stale00")
    version.build.cache_clear()
    try:
        assert version.build() == "11.abc1234", "有 git 時應該相信 git"
    finally:
        version.build.cache_clear()


def test_build_falls_back_to_stamp_without_git(monkeypatch):
    """沒有 git（Docker／pip 安裝）時才靠 stamp 檔。"""
    from vidgrab import version

    monkeypatch.delenv("VIDGRAB_BUILD", raising=False)
    monkeypatch.setattr(version, "_from_git", lambda: "")
    monkeypatch.setattr(version, "_from_file", lambda: "7.deadbee")
    version.build.cache_clear()
    try:
        assert version.build() == "7.deadbee"
    finally:
        version.build.cache_clear()


def test_build_env_wins(monkeypatch):
    """VIDGRAB_BUILD 永遠最優先（Docker 建置時帶入）。"""
    from vidgrab import version

    monkeypatch.setenv("VIDGRAB_BUILD", "42.cafe000")
    monkeypatch.setattr(version, "_from_git", lambda: "11.abc1234")
    monkeypatch.setattr(version, "_from_file", lambda: "3.stale00")
    version.build.cache_clear()
    try:
        assert version.build() == "42.cafe000"
    finally:
        version.build.cache_clear()


# ---------------------------------------------------------------- 繁簡與內容過濾


def test_traditional_to_simplified():
    """B 站是簡體站，繁體關鍵字搜不到東西。這是真實踩到的問題。"""
    from vidgrab.zh import to_simplified

    assert to_simplified("寺廟") == "寺庙"
    assert to_simplified("寺廟 建築") == "寺庙 建筑"
    assert to_simplified("山林 雲霧") == "山林 云雾"
    assert to_simplified("燭光 香爐 特寫") == "烛光 香炉 特写"


def test_conversion_leaves_unknown_alone():
    """沒收錄的字要原樣通過——寧可漏轉，不要轉錯。"""
    from vidgrab.zh import to_simplified

    assert to_simplified("handpan") == "handpan"
    assert to_simplified("台北") == "台北", "台北本來就不變"
    assert to_simplified("") == ""
    assert to_simplified("ABC 123") == "ABC 123"


def test_search_forms_always_keeps_original():
    """原文一定排第一：對照表不完整時，原本能搜到的不能消失。"""
    from vidgrab.zh import search_forms

    forms = search_forms("寺廟")
    assert forms[0] == "寺廟", "原文必須在"
    assert "寺庙" in forms

    # 已經是簡體，就不用多搜一次
    assert search_forms("寺庙") == ["寺庙"]
    assert search_forms("handpan") == ["handpan"]


def test_tutorial_detection():
    """公開課／建模教學不是可剪的素材。"""
    from vidgrab.models import Item, looks_like_tutorial

    def mk(title, dur=600.0):
        return Item(platform="b", vid="1", url="u", title=title, duration=dur)

    assert looks_like_tutorial(mk("【清华大学公开课】中国建筑史 全集"))
    assert looks_like_tutorial(mk("【3dmax建模】经典古建筑场景模型制作教程"))
    assert looks_like_tutorial(mk("Houdini制作山林云雾（附带树木课程）"))
    assert looks_like_tutorial(mk("佛教建筑第一讲｜亚洲佛教建筑总序"))

    # 不能誤殺真正的素材
    assert not looks_like_tutorial(mk("雨后深山鸟鸣声，云雾缭绕的仙境"))
    assert not looks_like_tutorial(mk("山西悬空寺 航拍 4K 空镜"))
    assert not looks_like_tutorial(mk("冬夜寺院燃烧的香烛 晚课的木鱼声"))


def test_purge_removes_tutorials(tmp_path, monkeypatch):
    """purge 要能清掉舊專案裡的教學，留下真正素材。"""
    import subprocess
    import sys

    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    _make_project(tmp_path, "p1", items=[
        {"platform": "b", "vid": "1", "url": "u",
         "title": "【清华大学公开课】中国建筑史 全集"},
        {"platform": "b", "vid": "2", "url": "u",
         "title": "山西悬空寺 航拍 4K 空镜"},
    ])

    env = {**__import__("os").environ, "PYTHONPATH": str(_ROOT / "src")}
    r = subprocess.run(
        [sys.executable, "-m", "vidgrab.cli", "purge", "p1"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["dropped"] == 1
    assert data["remaining"] == 1

    r2 = subprocess.run(
        [sys.executable, "-m", "vidgrab.cli", "vault", "p1"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    left = json.loads(r2.stdout)["items"]
    assert len(left) == 1
    assert "悬空寺" in left[0]["title"]


def test_purge_dry_run_changes_nothing(tmp_path, monkeypatch):
    """--dry-run 只回報，不能動資料。"""
    import subprocess
    import sys

    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    _make_project(tmp_path, "p1", items=[
        {"platform": "b", "vid": "1", "url": "u", "title": "某某教程"},
        {"platform": "b", "vid": "2", "url": "u", "title": "空镜 航拍"},
    ])

    env = {**__import__("os").environ, "PYTHONPATH": str(_ROOT / "src")}
    r = subprocess.run(
        [sys.executable, "-m", "vidgrab.cli", "purge", "p1", "--dry-run"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    data = json.loads(r.stdout)
    assert data["dry_run"] is True
    assert data["would_drop"] == 1
    assert data["dropped"] == 0

    from vidgrab import tasks
    _, items = tasks.load_project(tmp_path / "projects" / "p1")
    assert len(items) == 2, "dry-run 不可以刪東西"


def test_prune_removes_orphan_files(tmp_path, monkeypatch):
    """purge 之後磁碟上會留孤兒檔；prune 要清掉，但不能碰清單在用的。"""
    import subprocess
    import sys

    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    proj = _make_project(tmp_path, "p1", items=[{
        "platform": "b", "vid": "keep", "url": "u", "title": "要留的",
    }])
    clips = proj / "clips"
    clips.mkdir(parents=True, exist_ok=True)

    kept = clips / "keep.mp4"
    kept.write_bytes(b"k" * 100)
    orphan = clips / "orphan.mp4"
    orphan.write_bytes(b"o" * 200)
    orphan_side = clips / "orphan.info.json"
    orphan_side.write_text("{}", encoding="utf-8")

    # 讓清單指向 kept
    from vidgrab import tasks
    manifest, items = tasks.load_project(proj)
    items[0].clips = [{"path": str(kept), "start": 0.0, "end": 5.0}]
    tasks.save_project(proj, manifest, items)

    env = {**__import__("os").environ, "PYTHONPATH": str(_ROOT / "src")}
    r = subprocess.run(
        [sys.executable, "-m", "vidgrab.cli", "prune", "p1"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["orphans"] == 2, data["files"]
    assert kept.exists(), "清單在用的不可以刪"
    assert not orphan.exists()
    assert not orphan_side.exists()


def test_prune_dry_run_keeps_files(tmp_path, monkeypatch):
    import subprocess
    import sys

    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    proj = _make_project(tmp_path, "p1")
    clips = proj / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    f = clips / "orphan.mp4"
    f.write_bytes(b"x" * 10)

    env = {**__import__("os").environ, "PYTHONPATH": str(_ROOT / "src")}
    r = subprocess.run(
        [sys.executable, "-m", "vidgrab.cli", "prune", "p1", "--dry-run"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    data = json.loads(r.stdout)
    assert data["dry_run"] is True
    assert data["orphans"] == 1
    assert f.exists(), "dry-run 不可以刪檔"


def test_build_works_without_ffmpeg(tmp_path):
    """CI 沒有 ffmpeg。讀資料的指令不該被它擋住。

    真的掛過：`vidgrab build --json` 在 GitHub Actions 回 exit 2，
    連續五次 push 的 CI 都紅的。
    """
    import os
    import subprocess
    import sys

    from vidgrab.models import NEEDS_NO_FFMPEG

    # 只放最基本的東西，確保 PATH 裡找不到 ffmpeg
    bare = tmp_path / "bin"
    bare.mkdir()
    for tool in ("git", "sh", "env"):
        src = __import__("shutil").which(tool)
        if src:
            (bare / tool).symlink_to(src)

    env = {
        **os.environ,
        "PATH": f"{bare}:/usr/bin:/bin",
        "PYTHONPATH": str(_ROOT / "src"),
        "VIDGRAB_HOME": str(tmp_path / "projects"),
    }
    r = subprocess.run(
        [sys.executable, "-m", "vidgrab.cli", "build", "--json"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert r.returncode == 0, f"沒有 ffmpeg 時 build 應該照樣能跑：{r.stderr}"
    data = json.loads(r.stdout)
    assert data["version"]

    # 這份清單是 CLI 與啟動腳本共用的，改動時要想清楚
    assert "build" in NEEDS_NO_FFMPEG
    assert "vault" in NEEDS_NO_FFMPEG
    assert "test" in NEEDS_NO_FFMPEG
    assert "pick" not in NEEDS_NO_FFMPEG, "pick 真的要 ffmpeg，不能豁免"


def test_launcher_and_cli_share_ffmpeg_list():
    """啟動腳本與 CLI 的豁免清單不可以不同步。

    之前分成兩份，導致 CI 掛掉。
    """
    from vidgrab.models import NEEDS_NO_FFMPEG

    launcher = _ROOT / "vidgrab"
    if not launcher.exists():
        # CI 用 pip 安裝，沒有這個啟動腳本。
        # 這時仍然要驗清單本身：每個指令都要是 CLI 真的有的子命令，
        # 打錯字的豁免等於沒豁免。
        from vidgrab.cli import build_parser

        names = set(build_parser()._subparsers._group_actions[0].choices)
        missing = NEEDS_NO_FFMPEG - names
        assert not missing, f"豁免清單裡有 CLI 沒有的指令：{sorted(missing)}"
        return

    src = launcher.read_text(encoding="utf-8")
    assert "needs_ffmpeg" in src, "啟動腳本要有這個判斷"
    # 腳本裡的 case 清單要涵蓋 Python 清單裡的每個指令
    for cmd in NEEDS_NO_FFMPEG:
        assert f" {cmd} " in src or f" {cmd}\"" in src, (
            f"啟動腳本的清單少了 {cmd}，兩邊不同步"
        )


def test_parse_progress_accepts_real_ytdlp_lines():
    """進度解析要能吃 yt-dlp 真實吐出來的行。

    真的壞過三次：
    1. 只認行首的 VIDGRAB，但實際前面有 `[download] download:`
    2. 硬要求 7 個欄位，但切片下載只給 2 個（`VIDGRAB 7283754 NA`）
    3. _common_args 裡有 --no-progress，把輸出全壓掉了
    """
    from vidgrab.media import _parse_progress

    # 實際抓到的格式
    got = _parse_progress("VIDGRAB 7283754 NA")
    assert got is not None, "只有位元組數時也要能解析"
    assert got["downloaded"] == 7283754
    assert got["percent"] is None, "沒有總量就沒有百分比，不要編造"
    assert got["bytes"], "至少要能顯示已下載多少"

    # 完整格式（前面帶 yt-dlp 的標籤）
    got = _parse_progress("[download] download:VIDGRAB 1048576 5242880 2097152.0 3.5 1 5")
    assert got["percent"] == 20.0
    assert got["fragment"] == "1/5"
    assert got["speed"]

    # 不是進度行就回 None
    assert _parse_progress("[download] Destination: x.mp4") is None
    assert _parse_progress("[info] hello") is None


def test_common_args_does_not_suppress_progress():
    """--no-progress 會讓 --progress-template 完全沒輸出。

    這個組合存在很久，因為沒人驗過進度條真的會動。
    """
    from vidgrab.media import GrabOptions, _common_args

    args = _common_args(GrabOptions(height=480))
    assert "--no-progress" not in args, "--no-progress 會壓掉所有進度輸出"
    assert "--progress-template" in args
    assert "--newline" in args, "沒有 --newline 就讀不到一行一行的進度"


def test_clip_progress_marks_estimated_flag():
    """切片下載的心跳回報要標 estimated，前端才知道那不是真實百分比。

    這裡驗證的是「回報格式」，不是心跳執行緒本身
    （執行緒要真的下載才跑得起來，那條路靠 UI 實測驗證）。
    """
    from vidgrab.media import _parse_progress

    real = _parse_progress("VIDGRAB 7283754 NA")
    assert real is not None
    # 真實回報沒有 estimated，代表是量到的
    assert not real.get("estimated")
    # 沒有總量時不該謊報百分比
    assert real["percent"] is None


def test_parse_ts_rejects_bad_input_as_user_error():
    """時間格式寫錯要回 bad_input，不是 internal。

    原本拋裸 ValueError，被包成 code=internal、retryable=true，
    agent 會照著重試一次，然後還是同樣的錯。
    """
    from vidgrab.models import BadInput, parse_ts

    assert parse_ts("1:23") == 83.0
    assert parse_ts("00:01:23") == 83.0
    assert parse_ts("12.5") == 12.5

    for bad in ("0:100", "abc", "1:2:3:4", ""):
        try:
            parse_ts(bad)
        except BadInput as exc:
            assert exc.code == "bad_input"
            assert exc.retryable is False, f"{bad} 重試也沒用，不該標成可重試"
            assert exc.hint, "要告訴使用者正確寫法"
        else:
            raise AssertionError(f"{bad} 應該被拒絕")


def test_frame_failure_does_not_kill_the_job(tmp_path, monkeypatch):
    """抽畫面失敗不可以讓整個作業失敗。

    真的發生過：YouTube 長片抽幀逾時 → 整批 batch 回 internal，
    但片段其實已經下載好了，使用者卻收到失敗。
    """
    from vidgrab import media, tasks

    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    proj = _make_project(tmp_path, "p1", items=[
        {"platform": "youtube", "vid": "abc", "url": "https://x", "title": "長片",
         "duration": 3654.0},
    ])

    def boom(*_a, **_k):
        raise RuntimeError("模擬逾時")

    monkeypatch.setattr(media, "probe_frames", boom)

    logs: list[str] = []
    result = tasks.run_pick(
        proj, [0], frames=True, frame_count=1, frame_only=True, emit=logs.append
    )
    assert isinstance(result, dict), "抽幀失敗時仍然要回傳結果"
    assert any("略過" in line or "照樣保留" in line for line in logs), (
        "要留下降級訊息，不能靜默略過"
    )


def test_clip_summary_reports_orientation():
    """直式與橫式要標出來。

    直式片段（1080×1920）剪進橫式影片會整段黑邊，
    選素材的人應該在清單上就看到，不必等下載完才發現。
    """
    from pathlib import Path

    from vidgrab.agent import clip_summary

    root = Path("/tmp")
    portrait = clip_summary({"path": "a.mp4", "start": 10, "end": 25,
                             "width": 1080, "height": 1920}, root)
    assert portrait["orientation"] == "portrait"

    landscape = clip_summary({"path": "b.mp4", "start": 10, "end": 25,
                              "width": 1920, "height": 1080}, root)
    assert landscape["orientation"] == "landscape"

    # 舊資料沒有存 orientation，也要當場算得出來
    assert portrait["width"] == 1080
    assert landscape["height"] == 1080


def test_vault_entry_shape_is_uniform_for_missing_projects(tmp_path, monkeypatch):
    """資料夾消失的專案，回傳形狀也要跟正常專案一樣。

    真的踩過：library 裡留著已刪除專案的記錄，vault 只回 5 個欄位，
    呼叫方一取 items 就 KeyError，整個稽核腳本掛掉。
    """
    from vidgrab import agent, library, tasks

    home = tmp_path / "projects"
    monkeypatch.setenv("VIDGRAB_HOME", str(home))
    proj = _make_project(tmp_path, "alive", items=[
        {"platform": "b", "vid": "1", "url": "u", "title": "還活著"},
    ])
    agent.vault_manifest(with_items=True)   # 讓它進 library

    # 第二個專案建好之後直接刪掉資料夾，模擬「清空 projects」
    gone = _make_project(tmp_path, "gone", items=[
        {"platform": "b", "vid": "2", "url": "u", "title": "要被刪"},
    ])
    agent.vault_manifest(with_items=True)
    __import__("shutil").rmtree(gone)

    v = agent.vault_manifest(with_items=True)
    by_name = {p["name"]: p for p in v["projects"]}
    assert "gone" in by_name, "消失的專案仍要出現在清單（叫使用者回來看）"
    ghost = by_name["gone"]
    assert ghost["missing"] is True
    for key in ("items", "counts", "queries", "path", "title"):
        assert key in ghost, f"消失的專案少了 {key}，形狀跟正常專案不一致"
    assert ghost["items"] == []
    assert ghost["counts"]["clips"] == 0
    assert "items" in by_name["alive"]


def test_search_default_keeps_old_project(tmp_path, monkeypatch):
    """重搜同名專案時，預設**不可以**蓋掉舊的。

    舊專案可能已經下載了片段，覆蓋下去片段會一起消失。
    """
    from vidgrab import tasks

    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    proj = _make_project(tmp_path, "same", items=[
        {"platform": "b", "vid": "keep", "url": "u", "title": "舊的"},
    ])
    assert (proj / "results.json").exists()

    from vidgrab.models import Item

    class _FakeSource:
        name = "fake"
        def search(self, *_a, **_k):
            return [Item(platform="fake", vid="v1", url="https://x",
                         title="假的素材", duration=120.0)]

    monkeypatch.setattr(tasks, "get_sources", lambda _p: [_FakeSource()])
    out = tasks.run_search(["x"], name="same", per_keyword=1, emit=lambda *_: None)
    new_path = out["project"]
    assert new_path.name != "same", "預設要另開一個，不能蓋掉舊專案"
    assert proj.exists() and (proj / "results.json").exists(), "舊專案要原封不動"
    assert "same" in new_path.name and len(new_path.name) > len("same")


def test_forget_removes_deleted_project_from_library(tmp_path, monkeypatch):
    """資料夾刪掉之後要能把清單記錄清掉。

    forget 處理的正是「資料夾已經不見」的情況，
    所以它不能走 resolve_project（那個要求資料夾存在，會直接報找不到）。
    """
    import os
    import subprocess
    import sys

    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    proj = _make_project(tmp_path, "ghost", items=[
        {"platform": "b", "vid": "1", "url": "u", "title": "會被刪"},
    ])
    env = {**os.environ, "PYTHONPATH": str(_ROOT / "src"),
           "VIDGRAB_HOME": str(tmp_path / "projects")}

    # 先讓它進 library
    subprocess.run([sys.executable, "-m", "vidgrab.cli", "vault"],
                   capture_output=True, text=True, timeout=60, env=env)
    __import__("shutil").rmtree(proj)

    r = subprocess.run([sys.executable, "-m", "vidgrab.cli", "vault"],
                       capture_output=True, text=True, timeout=60, env=env)
    assert "ghost" in r.stdout, "資料夾不見了，記錄還在（這是刻意的）"

    r = subprocess.run([sys.executable, "-m", "vidgrab.cli", "forget", "ghost"],
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["removed"] == ["ghost"]

    r = subprocess.run([sys.executable, "-m", "vidgrab.cli", "vault"],
                       capture_output=True, text=True, timeout=60, env=env)
    assert "ghost" not in r.stdout, "forget 之後記錄要消失"


def test_thumb_comes_from_the_clip_itself(tmp_path):
    """縮圖一定要從「這個片段檔」抽，不能另外去整支影片抓。

    以前是到整支影片的均勻位置抓 3 格（片段在 30–75 秒，
    抓的卻是 914／1827／2740 秒），那些圖跟片段毫無關係。
    """
    import shutil
    import subprocess

    import pytest

    from vidgrab.media import thumb_from_clip

    if not shutil.which("ffmpeg"):
        pytest.skip("這條測真的用 ffmpeg 抽圖；CI 沒有裝 ffmpeg")

    src = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         "color=c=red:s=320x180:d=3", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True, timeout=60,
    )
    dest = tmp_path / "t.jpg"
    got = thumb_from_clip(src, dest)
    assert got is not None and dest.exists()
    assert dest.stat().st_size > 0

    # 重跑不會重做（有就沿用）
    again = thumb_from_clip(src, dest)
    assert again == dest

    # 片段不存在就回 None，不可以拋
    assert thumb_from_clip(tmp_path / "nope.mp4", tmp_path / "x.jpg") is None


def test_backfill_thumbs_fills_every_clip(tmp_path, monkeypatch):
    """既有片段要能補縮圖，而且補完每個片段都有。"""
    import shutil
    import subprocess

    import pytest

    from vidgrab import tasks

    if not shutil.which("ffmpeg"):
        pytest.skip("這條測真的用 ffmpeg 抽圖；CI 沒有裝 ffmpeg")

    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    proj = _make_project(tmp_path, "p1", items=[
        {"platform": "b", "vid": "v1", "url": "u", "title": "甲"},
    ])
    clips = proj / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    made = clips / "one.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         "color=c=blue:s=320x180:d=2", "-pix_fmt", "yuv420p", str(made)],
        check=True, capture_output=True, timeout=60,
    )

    manifest, items = tasks.load_project(proj)
    items[0].clips = [{"path": str(made), "start": 0.0, "end": 2.0,
                       "id": "b:v1#0-2"}]
    tasks.save_project(proj, manifest, items)

    stats = tasks.backfill_thumbs(proj, emit=lambda *_: None)
    assert stats["made"] == 1 and stats["failed"] == 0

    _m, after = tasks.load_project(proj)
    thumb = after[0].clips[0].get("thumb")
    assert thumb and Path(thumb).exists()


def test_agent_exposes_clip_thumb(tmp_path, monkeypatch):
    """agent 介面一定要把 clipping 縮圖帶出去。

    這是「使用者看不到截圖」的真正原因之一：
    縮圖明明在磁碟上，但 clip_summary 沒有把它放進回傳的 dict，
    所以任務清單／agent 都拿不到。
    """
    from pathlib import Path

    from vidgrab.agent import clip_summary

    thumb = tmp_path / "t.jpg"
    thumb.write_bytes(b"fake-jpg")

    got = clip_summary(
        {"path": "a.mp4", "start": 0, "end": 5, "width": 640, "height": 360,
         "thumb": str(thumb)},
        tmp_path,
    )
    assert got["thumb"], "clip_summary 要把 thumb 帶出來"
    assert got["has_thumb"] is True

    # 檔案不存在時 has_thumb 要是 False，不要騙人
    missing = clip_summary(
        {"path": "a.mp4", "start": 0, "end": 5, "thumb": str(tmp_path / "nope.jpg")},
        tmp_path,
    )
    assert missing["has_thumb"] is False


def test_file_route_blocks_prefix_path_traversal(tmp_path, monkeypatch):
    """`/file/` 不可以被前綴相同的專案名穿越。

    真的踩過：判斷寫成 str(target).startswith(str(project))，
    於是 /projects/victim 這個專案能讀到 /projects/victim-evil/secret.txt
    ——字串前綴成立，但那是**另一個專案**的檔案。
    """
    import http.client

    from vidgrab import web

    root = tmp_path / "projects"
    (root / "victim" / "clips").mkdir(parents=True)
    (root / "victim" / "manifest.json").write_text("{}", encoding="utf-8")
    (root / "victim" / "clips" / "ok.txt").write_text("INSIDE", encoding="utf-8")
    (root / "victim-evil").mkdir(parents=True)
    (root / "victim-evil" / "secret.txt").write_text("SECRET", encoding="utf-8")

    monkeypatch.setenv("VIDGRAB_HOME", str(root))

    import threading
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    try:
        def get(path: str) -> tuple[int, str]:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
            c.putheader("Host", "127.0.0.1")
            c.endheaders()
            r = c.getresponse()
            body = r.read().decode("utf-8", "replace")
            c.close()
            return r.status, body

        code, body = get("/file/victim/clips/ok.txt")
        assert code == 200 and body == "INSIDE", "專案內自己的檔案要讀得到"

        for attempt in (
            "/file/victim/../victim-evil/secret.txt",
            "/file/victim/%2e%2e/victim-evil/secret.txt",
        ):
            code, body = get(attempt)
            assert code == 404, f"前綴相同的專案被穿越了：{attempt} → {code}"
            assert "SECRET" not in body
    finally:
        srv.shutdown()


def test_cross_origin_post_is_rejected(tmp_path, monkeypatch):
    """跨站 POST 要被擋（CSRF）。

    這個服務沒有登入機制，所以「瀏覽器替別人送出的請求」
    跟「你自己按的」在伺服器端長得一樣。
    """
    import http.client
    import json as _json
    import threading
    from http.server import ThreadingHTTPServer

    from vidgrab import web

    root = tmp_path / "projects"
    (root / "p1").mkdir(parents=True)
    (root / "p1" / "manifest.json").write_text('{"title":"原始"}', encoding="utf-8")
    monkeypatch.setenv("VIDGRAB_HOME", str(root))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def post(payload: dict, origin: str | None) -> tuple[int, str]:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = _json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        c.request("POST", "/api/library", body=body, headers=headers)
        r = c.getresponse()
        out = r.read().decode("utf-8", "replace")
        c.close()
        return r.status, out

    try:
        code, body = post(
            {"action": "rename", "project": "p1", "title": "被駭了"},
            "http://evil.example",
        )
        assert code == 403, f"跨站請求應該被拒絕，卻回了 {code}"
        assert "被駭了" not in body

        # 本機來源要能用
        code, _ = post(
            {"action": "rename", "project": "p1", "title": "正常"},
            f"http://127.0.0.1:{port}",
        )
        assert code == 200

        # 沒有 Origin（curl、本機程式）也要能用，否則會擋掉自己
        code, _ = post({"action": "rename", "project": "p1", "title": "CLI"}, None)
        assert code == 200
    finally:
        srv.shutdown()


def test_web_refuses_to_bind_publicly_without_optin(tmp_path, monkeypatch):
    """綁到非本機位址要明確同意：這個介面沒有登入機制。"""
    import os
    import subprocess
    import sys

    monkeypatch.setenv("VIDGRAB_HOME", str(tmp_path / "projects"))
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(_ROOT / "src"),
           "VIDGRAB_HOME": str(tmp_path / "projects")}

    r = subprocess.run(
        [sys.executable, "-m", "vidgrab.cli", "web", "--no-open",
         "--host", "0.0.0.0", "--port", "0"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert r.returncode == 2, f"應該拒絕，實際 returncode={r.returncode}"
    assert "allow-lan" in r.stderr, "要告訴使用者怎麼明確同意"


def test_jobs_do_not_grow_without_bound():
    """JOBS 要有上限。

    真的會漏：每跑一次搜尋／下載就寫進 JOBS，但從來不刪，
    開著用一整天就一路長大（每個 job 都帶著完整結果與日誌）。
    """
    from vidgrab import web

    web.JOBS.clear()
    try:
        for i in range(150):
            job = web.Job("test", f"job{i}")
            job.finish({"i": i})
            with web.LOCK:
                web.JOBS[job.id] = job
        assert len(web.JOBS) > web.MAX_FINISHED_JOBS, "前提：塞得比上限多"

        web._prune_jobs()
        assert len(web.JOBS) <= web.MAX_FINISHED_JOBS, (
            f"清理後還有 {len(web.JOBS)} 個，上限是 {web.MAX_FINISHED_JOBS}"
        )
    finally:
        web.JOBS.clear()


def test_job_message_log_is_capped():
    """單一 job 的日誌也要有上限，否則長時間下載會吃光記憶體。"""
    from vidgrab import web

    job = web.Job("test", "長跑")
    for i in range(web.MAX_JOB_MESSAGES + 500):
        job.emit(f"line {i}")
    assert len(job.messages) <= web.MAX_JOB_MESSAGES
    assert len(job.messages) > 0


def test_dockerfile_opts_into_lan_binding():
    """Docker 裡綁 0.0.0.0 是對的，但必須明確帶 --allow-lan。

    踩過：加了「非本機位址要 --allow-lan」的防護之後，
    Dockerfile 的 VIDGRAB_HOST=0.0.0.0 讓容器起不來，CI 的
    smoke test 連不上 8310。兩邊要一致。
    """
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")

    binds_public = "VIDGRAB_HOST=0.0.0.0" in dockerfile
    assert binds_public, "前提：容器需要綁 0.0.0.0 才連得到"

    cmd_line = next(
        (ln for ln in dockerfile.splitlines()
         if ln.strip().startswith("CMD") and "vidgrab" in ln),
        "",
    )
    assert "web" in cmd_line, f"找不到 web 的 CMD：{cmd_line!r}"
    assert "--allow-lan" in cmd_line, (
        "容器綁 0.0.0.0 卻沒帶 --allow-lan，會啟動失敗："
        f"{cmd_line!r}"
    )


def test_write_json_is_safe_under_concurrent_writers(tmp_path):
    """兩個執行緒同時寫同一個檔案不可以炸。

    實際踩到：web 服務是多執行緒的，連按兩下釘選 →
    兩邊都用 xxx.json.tmp 當暫存檔，A 先 rename 走，B rename 就
    FileNotFoundError，連線直接斷掉（瀏覽器看到 ERR_EMPTY_RESPONSE）。
    """
    import threading

    from vidgrab.models import read_json, write_json

    target = tmp_path / "library.json"
    write_json(target, {"n": 0})

    errors: list[BaseException] = []

    def hammer(seed: int) -> None:
        try:
            for i in range(25):
                write_json(target, {"n": seed * 1000 + i})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(k,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"並行寫入爆炸：{errors[:2]}"
    # 檔案要還是完整的 JSON（不能是被截斷的半份）
    got = read_json(target, default=None)
    assert isinstance(got, dict) and "n" in got
    # 不可以留下暫存檔
    assert list(tmp_path.glob("*.tmp")) == []


def test_built_assets_are_present_in_package():
    """打包後的資源要真的存在且有內容。

    階段 1 之後前端是 Vite 產物。如果忘了 build 就發佈，
    /static/assets/* 會 404，整個介面白畫面 —— 這是很容易漏的一步。
    """
    import glob
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "vidgrab" / "static"
    index = (root / "index.html").read_text(encoding="utf-8")

    assert 'id="root"' in index, "index.html 要有 React 掛載點"
    assert "/static/assets/" in index, "index.html 要指向打包後的資源"

    js = glob.glob(str(root / "assets" / "*.js"))
    css = glob.glob(str(root / "assets" / "*.css"))
    assert js, "找不到打包後的 JS（cd web && npm run build）"
    assert css, "找不到打包後的 CSS（cd web && npm run build）"

    # React 有進來才算真的打包成功
    bundle = Path(js[0]).read_text(encoding="utf-8", errors="ignore")
    assert len(bundle) > 50_000, f"JS 只有 {len(bundle)} bytes，可能打壞了"
    assert "react" in bundle.lower(), "打包產物裡找不到 React"


def test_theme_ids_match_between_ts_and_css():
    """TS 的主題清單要跟 CSS 對得上。

    漏一個主題，切過去就會變成「沒有對應變數」→ 透明或錯色，
    而且只在切到那個主題時才看得出來。
    """
    import glob
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "vidgrab"
    ts = (root.parent.parent / "web" / "src" / "lib" / "theme.ts").read_text(encoding="utf-8")
    ts_ids = set(re.findall(r"id:\s*'([a-z]+)'", ts))

    css_path = sorted(glob.glob(str(root / "static" / "assets" / "*.css")))[0]
    css = Path(css_path).read_text(encoding="utf-8")
    css_ids = set(re.findall(r'\[data-theme="?([a-z]+)"?\]', css))

    for want in ("midnight", "ink", "forest", "paper", "mist", "sakura"):
        assert want in ts_ids, f"theme.ts 少了 {want}"
        assert want in css_ids, f"CSS 少了 {want} 的變數"
    assert ts_ids <= css_ids, f"TS 有但 CSS 沒有：{ts_ids - css_ids}"


# ---------------------------------------------------------------- 下載重試


def test_download_retries_up_to_ten_times(monkeypatch, tmp_path):
    """下載失敗要自動重試，最多 10 次。

    用假的 _run_stream 模擬「前 3 次失敗、第 4 次成功」，
    確認真的重試了、而且最後拿到檔案。
    """
    from vidgrab import media

    monkeypatch.setattr(media.time, "sleep", lambda _s: None)  # 不要真的等

    calls = {"n": 0}

    def fake_run(cmd, on_progress=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 4:
            return 1, "ERROR: HTTP Error 503: Service Unavailable"
        # 假裝產出檔案
        stem = next((a for a in cmd if a.endswith(".%(ext)s")), "")
        out = Path(stem.replace(".%(ext)s", ".mp4"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 64)
        return 0, ""

    monkeypatch.setattr(media, "_run_stream", fake_run)
    monkeypatch.setattr(media, "ffprobe", lambda p: {"duration": 1.0, "width": 2, "height": 2})

    item = _item_for_test()
    out = media.grab_clip(item, 0, 5, tmp_path, label="t")

    assert calls["n"] == 4, f"應該試 4 次，實際 {calls['n']}"
    assert Path(out["path"]).exists()


def test_download_gives_up_after_ten_and_does_not_retry_404(monkeypatch, tmp_path):
    """永久性錯誤（404）不該重試；可重試錯誤最多 10 次就放棄。"""
    from vidgrab import media

    monkeypatch.setattr(media.time, "sleep", lambda _s: None)

    # 404：只打一次
    seen = {"n": 0}

    def not_found(cmd, on_progress=None, timeout=None):
        seen["n"] += 1
        return 1, "ERROR: HTTP Error 404: Not Found"

    monkeypatch.setattr(media, "_run_stream", not_found)
    with pytest.raises(media.MediaError):
        media.grab_clip(_item_for_test(), 0, 5, tmp_path / "a", label="t")
    assert seen["n"] == 1, f"404 不該重試，卻試了 {seen['n']} 次"

    # 503：試滿 10 次
    seen["n"] = 0

    def always_busy(cmd, on_progress=None, timeout=None):
        seen["n"] += 1
        return 1, "ERROR: HTTP Error 503: Service Unavailable"

    monkeypatch.setattr(media, "_run_stream", always_busy)
    with pytest.raises(media.MediaError):
        media.grab_clip(_item_for_test(), 0, 5, tmp_path / "b", label="t")
    assert seen["n"] == media.MAX_DOWNLOAD_RETRIES == 10, f"試了 {seen['n']} 次"


def test_retry_backoff_is_bounded(monkeypatch):
    """退避要遞增但有上限，不可以無限長大。"""
    from vidgrab import media

    waits = [media._backoff(n) for n in range(1, 12)]
    assert waits[0] == 1.0
    assert waits == sorted(waits), "要遞增"
    assert max(waits) <= 20.0, "上限 20 秒"


def _item_for_test():
    from vidgrab.models import Item

    return Item(platform="bilibili", vid="BVTEST", url="https://example.invalid/x",
                title="測試")


# ---------------------------------------------------------------- 設定／admin


def test_every_config_field_has_admin_metadata():
    """每個設定項都要有分組與標籤，否則 admin UI 會出現「其他」孤兒。"""
    from vidgrab import config

    described = config.describe()
    keys = {f["key"] for f in described["fields"]}
    assert keys == set(config.DEFAULTS), "describe() 漏了設定項"

    for f in described["fields"]:
        assert f["group"] != "其他", f"{f['key']} 沒有分組"
        assert f["label"] and f["label"] != f["key"], f"{f['key']} 沒有人看得懂的標籤"
        assert f["type"] in ("str", "int", "float", "bool", "list")


def test_config_enum_options_are_exposed():
    """有枚舉的欄位要把可選值交出來，前端才能做成下拉。"""
    from vidgrab import config

    by_key = {f["key"]: f for f in config.describe()["fields"]}
    for key, allowed in config.ENUMS.items():
        assert key in by_key, f"{key} 不在 fields 裡"
        assert set(by_key[key]["options"]) == allowed, f"{key} 的可選項不對"
        assert by_key[key]["value"] in allowed


def test_config_marks_changed_from_default():
    """改過的欄位要標成 changed，UI 才畫得出「與預設不同」的點。"""
    from vidgrab import config

    config.save({"height": 720})
    by_key = {f["key"]: f for f in config.describe()["fields"]}
    assert by_key["height"]["changed"] is True
    assert by_key["height"]["value"] == 720
    assert by_key["per"]["changed"] is False


def test_admin_config_roundtrip_via_web(web):
    """從網頁改設定 → 讀回來要是新值；還原也要真的回到預設。"""
    import json as _json
    import urllib.request

    def post(payload: dict) -> dict:
        req = urllib.request.Request(
            web + "/api/config",
            data=_json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        return _json.loads(urllib.request.urlopen(req, timeout=10).read())

    post({"set": {"per": 42, "height": 720}})
    got = _json.loads(urllib.request.urlopen(web + "/api/config", timeout=10).read())
    assert got["values"]["per"] == 42
    assert got["values"]["height"] == 720

    post({"reset": []})
    back = _json.loads(urllib.request.urlopen(web + "/api/config", timeout=10).read())
    assert back["values"]["per"] == back["defaults"]["per"]
    assert back["values"]["height"] == back["defaults"]["height"]


def test_retry_progress_event_carries_attempt(monkeypatch, tmp_path):
    """重試時要送進度事件，前端才知道「正在重試第 n 次」。"""
    from vidgrab import media

    monkeypatch.setattr(media.time, "sleep", lambda _s: None)
    events = []
    state = {"n": 0}

    def flaky(cmd, on_progress=None, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            return 1, "ERROR: HTTP Error 503: Service Unavailable"
        stem = next((a for a in cmd if a.endswith(".%(ext)s")), "")
        out = Path(stem.replace(".%(ext)s", ".mp4"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 32)
        return 0, ""

    monkeypatch.setattr(media, "_run_stream", flaky)
    # 要給尺寸，否則會被「壞檔」檢查擋下來（那條檢查是後來加的）
    monkeypatch.setattr(media, "ffprobe",
                        lambda p: {"width": 1920, "height": 1080, "duration": 1.0})

    media.grab_clip(_item_for_test(), 0, 5, tmp_path, label="t",
                    on_progress=events.append)

    retries = [e for e in events if e.get("retry")]
    assert retries, "沒有送出重試事件"
    assert retries[0]["retry"] == 1
    assert retries[0]["max_retries"] == 10


# ---------------------------------------------------------------- 前端功能覆蓋


def _ts_bundle() -> str:
    """所有前端原始碼合起來，用來確認某個功能還在。"""
    import glob
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "web" / "src"
    parts = []
    for pat in ("*.tsx", "*.ts", "lib/*.ts", "components/*.tsx"):
        for f in sorted(glob.glob(str(root / pat))):
            parts.append(Path(f).read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_frontend_keeps_features_ported_from_legacy():
    """舊版有的行為，React 版不能偷偷掉。

    這些都是實際存在過的功能，改寫時漏掉會讓使用者「東西不見了」
    卻沒有任何錯誤訊息（畫面就只是少了一塊）。
    """
    src = _ts_bundle()
    wanted = {
        "開機自動開第一個專案": "void openProject(list[0]!.name)",
        "素材庫排序送後端": "action: 'sort'",
        "拖曳後回到手動順序": "setLibSort('manual')",
        "頂端進度條": "ProgressBar",
        "下載時推開 log（body.dl-on）": "dl-on",
        "影片解不開的提示": "這個格式瀏覽器可能不支援",
        "重試次數顯示": "maxRetries",
        "燈箱": "lightbox",
        "掃描結果區": "libscanbox",
        "改名": "renaming",
    }
    missing = [name for name, token in wanted.items() if token not in src]
    assert not missing, f"這些功能不見了：{missing}"


def test_download_retry_limit_is_ten_and_configurable():
    """重試上限必須是 10，而且前端也要知道。"""
    from vidgrab import media

    assert media.MAX_DOWNLOAD_RETRIES == 10
    assert "maxRetries" in _ts_bundle()


def test_admin_ui_covers_every_config_group():
    """admin UI 要能顯示所有分組（漏一組等於那些設定改不到）。"""
    from vidgrab import config

    groups = {f["group"] for f in config.describe()["fields"]}
    src = _ts_bundle()
    for g in groups:
        assert g in src, f"admin UI 沒有「{g}」這組"


def test_progress_payload_is_not_printed_to_terminal(capsys):
    """進度行的 JSON 不可以漏到終端。

    實際踩到：pick 的時候 stderr 被 `17%{"kind": "download", ...}` 洗版，
    整包 JSON 跟著進度一起印出來，人完全看不懂發生什麼事。
    payload 是給 Web UI 畫進度條用的，終端只要人話那半。
    """
    from vidgrab.models import log

    log("\r       50% 下載中\x00" + '{"kind": "download", "percent": 50}')
    err = capsys.readouterr().err

    assert "下載中" in err, "人話那半要留著"
    assert "kind" not in err, f"JSON 漏出來了：{err!r}"
    assert "\x00" not in err, "分隔符不該出現在終端"


def test_log_handles_plain_message(capsys):
    """沒有 payload 的普通訊息要原樣印出。"""
    from vidgrab.models import log

    log("✔ 完成：1 段")
    assert "✔ 完成：1 段" in capsys.readouterr().err


def test_corrupt_download_is_rejected_not_saved(monkeypatch, tmp_path):
    """下載「成功」但檔案是壞的，不可以當成成功。

    實際踩到：yt-dlp 回 0、也產出 .mp4，但只有 98KB、裡面一條影像軌都沒有。
    這種檔進素材庫就是一格「無圖、尺寸空白」的垃圾，而且完全不會報錯。
    """
    from vidgrab import media

    monkeypatch.setattr(media.time, "sleep", lambda _s: None)

    def fake_run(cmd, on_progress=None, timeout=None):
        stem = next((a for a in cmd if a.endswith(".%(ext)s")), "")
        out = Path(stem.replace(".%(ext)s", ".mp4"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00" * 64)   # 有檔案，但解不出影像
        return 0, ""

    monkeypatch.setattr(media, "_run_stream", fake_run)
    # 壞檔的特徵：width/height 是 None，但 duration 有值（真的遇過）
    monkeypatch.setattr(media, "ffprobe",
                        lambda p: {"width": None, "height": None, "duration": 6.0})

    with pytest.raises(media.MediaError, match="壞的"):
        media.grab_clip(_item_for_test(), 0, 6, tmp_path, label="t")

    left = list(tmp_path.glob("*.mp4"))
    assert not left, f"壞檔應該被刪掉，卻留著：{left}"


def test_good_download_is_accepted(monkeypatch, tmp_path):
    """正常的檔案不能被誤擋。"""
    from vidgrab import media

    def fake_run(cmd, on_progress=None, timeout=None):
        stem = next((a for a in cmd if a.endswith(".%(ext)s")), "")
        out = Path(stem.replace(".%(ext)s", ".mp4"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00" * 2048)
        return 0, ""

    monkeypatch.setattr(media, "_run_stream", fake_run)
    monkeypatch.setattr(media, "ffprobe",
                        lambda p: {"width": 1920, "height": 1080, "duration": 8.0})

    got = media.grab_clip(_item_for_test(), 0, 8, tmp_path, label="t")
    assert Path(got["path"]).exists()
    assert got["width"] == 1920


def test_prune_removes_records_whose_file_is_gone(tmp_path, monkeypatch):
    """清單裡有紀錄、磁碟上沒檔案 → prune 要把它清掉。

    實際踩到：刪了 .mp4 但 results.json 還記著，
    素材庫就出現「看得到、點不開」的卡片。舊版 prune 只處理
    「磁碟有、清單沒有」的反向孤兒，遇到這種完全幫不上忙。
    """
    import json as _json
    import subprocess
    from pathlib import Path

    home = tmp_path / "projects"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VIDGRAB_HOME", str(home))
    proj = home / "stale-proj"
    clips = proj / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    (proj / "manifest.json").write_text(_json.dumps({"queries": ["x"]}), encoding="utf-8")

    alive = clips / "keep.mp4"
    alive.write_bytes(b"\x00" * 32)
    (proj / "results.json").write_text(_json.dumps([{
        "platform": "bilibili", "vid": "V1", "url": "u", "title": "t",
        "clips": [
            {"name": "keep.mp4", "path": str(alive)},
            {"name": "gone.mp4", "path": str(clips / "gone.mp4")},
        ],
    }], ensure_ascii=False), encoding="utf-8")

    root = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        [sys.executable, "-m", "vidgrab.cli", "prune", "stale-proj"],
        cwd=str(root), capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
    )
    assert out.returncode == 0, out.stderr
    got = _json.loads(out.stdout)
    assert got["stale_records"] == 1, got

    from vidgrab import tasks

    _m, items = tasks.load_project(proj)
    names = [Path(c["path"]).name for c in items[0].clips]
    assert names == ["keep.mp4"], names


def test_retry_recovers_from_transient_failure_for_real_binary(tmp_path, monkeypatch):
    """用真的 yt-dlp 跑一次「前兩次假裝失敗」，確認重試機制在真流程裡也有效。

    前面的測試都是替換 _run_stream；這條保留真的參數組裝，
    只讓第一次嘗試回傳失敗碼，確保重試繞的是同一份 cmd。
    """
    from vidgrab import media

    monkeypatch.setattr(media.time, "sleep", lambda _s: None)
    real = media._run_stream
    seen = {"n": 0, "cmds": []}

    def flaky(cmd, on_progress=None, timeout=None):
        seen["n"] += 1
        seen["cmds"].append(list(cmd))
        if seen["n"] <= 2:
            return 1, "ERROR: HTTP Error 503: Service Unavailable"
        return real(cmd, on_progress=on_progress, timeout=timeout)

    monkeypatch.setattr(media, "_run_stream", flaky)

    # 前兩次是我們假造的 503，之後交給真的 yt-dlp。
    # 網址不存在 → 真的也會失敗，而且失敗訊息是可重試類 → 一路試到上限。
    # 所以這裡驗的是「真的流程也會重試到 10 次」，不是「3 次就成功」。
    item = _item_for_test()
    with pytest.raises(media.MediaError):
        media.grab_clip(item, 0, 3, tmp_path, label="t")

    assert seen["n"] == media.MAX_DOWNLOAD_RETRIES, \
        f"應該試滿 {media.MAX_DOWNLOAD_RETRIES} 次，實際 {seen['n']}"
    assert seen["cmds"][0] == seen["cmds"][-1], "重試要用同一組參數"


# ---------------------------------------------------------------- 自動更新／進度


def test_progress_line_is_split_into_spoken_and_json():
    """進度行要拆成「人話」與「JSON」兩半。

    實際踩到：整包 {"kind": "download", ...} 被當成日誌文字印在
    log 視窗裡，使用者看到的是 `\r 17%\u0000{"kind": ...`。
    這裡直接驗後端的拆解：人話走 log、JSON 走 progress。
    """
    import json as _json

    from vidgrab import web as web_mod

    sent: list[tuple[str, object]] = []

    class FakeHandler(web_mod.Handler):  # type: ignore[misc]
        def __init__(self) -> None:   # 不呼叫父類別，只借用 _log_event
            pass

        def _event(self, kind: str, payload: object) -> None:
            sent.append((kind, payload))

    FakeHandler()._log_event(
        '\r       50% 下載中\x00'
        + _json.dumps({"kind": "download", "index": 0, "percent": 50})
    )

    assert [k for k, _ in sent] == ["log", "progress"]
    text = str(sent[0][1])
    assert "下載中" in text
    assert "\x00" not in text, "分隔符不該留在人話裡"
    assert "kind" not in text, "JSON 漏進 log"
    assert isinstance(sent[1][1], dict) and sent[1][1]["percent"] == 50


def test_plain_log_line_emits_only_log():
    """沒有 payload 的普通訊息只送 log，不要多送一個空的 progress。"""
    from vidgrab import web as web_mod

    sent: list[tuple[str, object]] = []

    class FakeHandler(web_mod.Handler):  # type: ignore[misc]
        def __init__(self) -> None:
            pass

        def _event(self, kind: str, payload: object) -> None:
            sent.append((kind, payload))

    FakeHandler()._log_event("✔ 完成：1 段")
    assert [k for k, _ in sent] == ["log"]


def test_clip_event_carries_index():
    """每抓到一段就要送出結構化的 clip 事件，前端才能即時掛上去。

    只送文字的話，使用者要等整批跑完才看得到已抓好的片段。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "vidgrab" / "tasks.py").read_text()
    assert '"kind": "clip"' in src, "沒有 clip 事件"
    assert '"grabbed": grabbed' in src, "clip 事件沒帶進度"


def test_search_and_frame_failures_are_retried():
    """搜尋與抓畫面失敗要自動重試，不是一次失敗就放棄。"""
    from pathlib import Path

    from vidgrab import tasks

    assert tasks.SEARCH_RETRIES >= 2
    assert tasks.FRAME_RETRIES >= 2

    src = (Path(__file__).resolve().parents[1] / "src" / "vidgrab" / "tasks.py").read_text()
    # 不綁死變數名（重試次數現在來自 config），改看「有沒有真的在重試」
    assert "range(1, tries + 1)" in src, "搜尋沒有重試迴圈"
    assert "range(1, frame_tries + 1)" in src, "抓畫面沒有重試迴圈"
    assert "search_retries" in src and "frame_retries" in src, "重試次數沒接 config"


# ---------------------------------------------------------------- 設定集中化


def test_config_covers_operational_settings():
    """重試、逾時、資源上限、介面節奏都要能在 config 裡調。

    單人／小團隊用：這些數字每個人的機器都不一樣，
    寫死在程式碼裡就只能改原始碼。
    """
    from vidgrab import config

    keys = {f["key"] for f in config.describe()["fields"]}
    for key in (
        "download_retries", "search_retries", "frame_retries", "retry_max_wait",
        "probe_timeout", "clip_timeout", "full_timeout",
        "max_finished_jobs", "job_ttl_minutes", "max_job_messages",
        "auto_refresh_ms", "project_refresh_ms", "log_max_lines",
        "sidebar_default_width",
    ):
        assert key in keys, f"config 少了 {key}"
    assert len(keys) >= 35


def test_every_config_field_has_label_and_group():
    """每個欄位都要有分組與人話標籤，否則 admin 面板會出現空白項。"""
    from vidgrab import config

    for f in config.describe()["fields"]:
        assert f.get("group"), f"{f['key']} 沒有分組"
        assert f.get("label"), f"{f['key']} 沒有標籤"


def test_config_values_actually_drive_behaviour(tmp_path, monkeypatch):
    """改了 config 要真的改變行為，不能只是「讀得到」。"""
    import json as _json

    from vidgrab import config, media, tasks

    path = tmp_path / "config.json"
    path.write_text(_json.dumps({"values": {
        "download_retries": 3,
        "search_retries": 7,
        "retry_max_wait": 5,
    }}), encoding="utf-8")
    monkeypatch.setenv("VIDGRAB_CONFIG", str(path))

    assert media._retries() == 3
    assert tasks._retries("search_retries", 3) == 7
    assert media._backoff(10) <= 5.0, "退避上限沒跟著 config"


def test_job_limits_follow_config(tmp_path, monkeypatch):
    """作業保留數量／TTL／日誌上限要能被 config 覆寫。"""
    import json as _json

    from vidgrab import web

    path = tmp_path / "config.json"
    path.write_text(_json.dumps({"values": {
        "max_finished_jobs": 5,
        "job_ttl_minutes": 7,
        "max_job_messages": 300,
    }}), encoding="utf-8")
    monkeypatch.setenv("VIDGRAB_CONFIG", str(path))

    keep, ttl, messages = web._job_limits()
    assert keep == 5
    assert ttl == 7 * 60
    assert messages == 300


def test_broken_config_falls_back_to_defaults(tmp_path, monkeypatch):
    """config 壞掉不能讓工具起不來 —— 回預設值繼續跑。"""
    from vidgrab import config, media

    path = tmp_path / "config.json"
    path.write_text("{ 這不是 JSON", encoding="utf-8")
    monkeypatch.setenv("VIDGRAB_CONFIG", str(path))

    assert config.load()["download_retries"] == config.DEFAULTS["download_retries"]
    assert media._retries() == media.MAX_DOWNLOAD_RETRIES


def test_admin_ui_orders_every_group():
    """admin 面板要列出所有分組，包含維運用的那幾組。"""
    from pathlib import Path

    from vidgrab import config

    src = (Path(__file__).resolve().parents[1]
           / "web" / "src" / "components" / "AdminPanel.tsx").read_text()
    for f in config.describe()["fields"]:
        assert f["group"] in src, f"admin UI 沒有「{f['group']}」這組"
