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
