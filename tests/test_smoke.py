"""煙霧測試：不打網路，只驗證「裝起來能跑、關鍵端點不錯」。

跑法：
    ./vidgrab test       或   pytest -q
"""

from __future__ import annotations

import json
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
    with urllib.request.urlopen(web + "/", timeout=5) as resp:
        body = resp.read().decode()
    assert "vidgrab" in body
    assert 'id="mode"' in body  # 整支／切片選單


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
    (tmp_path / "projects").mkdir(parents=True)
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


def _page_css() -> str:
    """把 web.py 裡那一大段 PAGE 字串抽出來，方便對 CSS 做靜態檢查。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "vidgrab" / "web.py").read_text(
        encoding="utf-8"
    )
    return src


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

    src = (Path(__file__).resolve().parents[1] / "src" / "vidgrab" / "web.py").read_text(
        encoding="utf-8"
    )
    themes = re.findall(r'\[data-theme="([a-z]+)"\]\s*\{(.*?)\}', src, re.S)
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
    src = (_ROOT / "vidgrab").read_text(encoding="utf-8")
    from vidgrab.models import NEEDS_NO_FFMPEG

    assert "needs_ffmpeg" in src, "啟動腳本要有這個判斷"
    # 腳本裡的 case 清單要涵蓋 Python 清單裡的每個指令
    for cmd in NEEDS_NO_FFMPEG:
        assert f" {cmd} " in src or f" {cmd}\"" in src, (
            f"啟動腳本的清單少了 {cmd}，兩邊不同步"
        )
