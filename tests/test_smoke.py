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
