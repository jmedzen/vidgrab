"""音樂庫（Openverse／Jamendo）與音訊下載、只要聲音的修正。全部離線。"""

from __future__ import annotations

import functools
import http.server
import subprocess
import threading
from pathlib import Path

import pytest

from vidgrab import media, tasks
from vidgrab.models import Item, contact_block
from vidgrab.music import (
    JamendoSource,
    OpenverseSource,
    classify,
    jamendo_key,
    license_code_from_url,
)
from vidgrab.sources import SourceError, get_source, get_sources

needs_ffmpeg = pytest.mark.skipif(not media.ffmpeg_ok(), reason="需要 ffmpeg")


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _src(src, monkeypatch, payload, status=200, seen=None):
    def fake_get(url, params=None, timeout=None):
        if seen is not None:
            seen.append((url, params))
        return _Resp(payload, status)

    monkeypatch.setattr(src.session, "get", fake_get)
    return src


OV = {
    "id": "abc-1", "title": "Calm Piano", "creator": "Ann", "creator_url": "https://ann.example",
    "url": "https://cdn.example/a.mp3", "foreign_landing_url": "https://source.example/a",
    "license": "by", "license_version": "4.0", "duration": 95000,
    "tags": [{"name": "piano"}, {"name": "calm"}], "genres": ["ambient"],
    "attribution": '"Calm Piano" by Ann is licensed under CC BY 4.0.', "thumbnail": "https://t/a.jpg",
}


def _ov(**kw):
    return dict(OV, **kw)


def test_license_helpers():
    assert license_code_from_url("http://creativecommons.org/licenses/by-nc-sa/3.0/") == "by-nc-sa"
    assert license_code_from_url("https://creativecommons.org/publicdomain/zero/1.0/") == "cc0"
    assert license_code_from_url("") == ""
    assert classify("cc0")[0] == "free"
    assert classify("by", "4.0") == ("free-attribution", "CC BY 4.0", "CC BY：可營利，需標註作者")
    for bad in ("by-nc", "by-nd", "by-sa", "by-nc-sa", "sampling+", ""):
        assert classify(bad) is None


def test_openverse_maps_fields_and_keeps_only_safe_licenses(monkeypatch):
    seen: list = []
    payload = {"results": [
        OV,
        _ov(id="x2", license="by-nc"),
        _ov(id="x3", license="by-sa"),
        _ov(id="x4", license="cc0", url=""),
        _ov(id="x5", license="cc0"),
    ]}
    src = _src(OpenverseSource(), monkeypatch, payload, seen=seen)
    items = src.search("calm piano", limit=5)
    assert [i.vid for i in items] == ["abc-1", "x5"]
    first = items[0]
    assert first.asset_id() == "openverse:abc-1" and first.title == "Calm Piano"
    assert first.duration == 95.0 and first.media_url == "https://cdn.example/a.mp3"
    assert first.license_kind == "free-attribution" and first.license_name == "CC BY 4.0"
    assert first.tags == ["piano", "calm", "ambient"]
    assert first.is_music() and first.is_stock()
    assert "Ann（CC BY 4.0）" in contact_block(first)["credit_hint"]
    assert items[1].license_kind == "free"
    url, params = seen[0]
    assert params["category"] == "music" and params["license_type"] == "commercial,modification"
    assert params["q"] == "calm piano"


def test_openverse_filters_duration_and_exclude(monkeypatch):
    src = _src(OpenverseSource(), monkeypatch, {"results": [OV]})
    assert src.search("x", min_seconds=120) == []
    assert src.search("x", max_seconds=60) == []
    assert src.search("x", exclude=["piano"]) == []
    assert len(src.search("x", min_seconds=30, max_seconds=300)) == 1


@pytest.mark.parametrize("status,needle", [(401, "拒絕"), (429, "限流"), (503, "暫時不可用")])
def test_openverse_http_errors(monkeypatch, status, needle):
    src = _src(OpenverseSource(), monkeypatch, {}, status=status)
    with pytest.raises(SourceError, match=needle):
        src.search("x")


JM = {
    "id": "777", "name": "Sunny Day", "duration": 140, "artist_id": "9", "artist_name": "Bob",
    "license_ccurl": "http://creativecommons.org/licenses/by/3.0/",
    "audiodownload": "https://cdn.jamendo/777.mp3", "audiodownload_allowed": True,
    "shareurl": "https://www.jamendo.com/track/777", "image": "https://img/777.jpg",
    "musicinfo": {"tags": {"genres": ["pop"], "instruments": ["guitar"], "vartags": ["happy"]}},
}


def test_jamendo_missing_key(monkeypatch):
    monkeypatch.delenv("JAMENDO_CLIENT_ID", raising=False)
    monkeypatch.delenv("VIDGRAB_JAMENDO_KEY", raising=False)
    assert jamendo_key() == ""
    with pytest.raises(SourceError, match="JAMENDO_CLIENT_ID"):
        get_source("jamendo")


def test_jamendo_maps_fields_and_filters(monkeypatch):
    seen: list = []
    payload = {"headers": {"status": "success", "code": 0}, "results": [
        JM,
        dict(JM, id="2", license_ccurl="http://creativecommons.org/licenses/by-nc-sa/3.0/"),
        dict(JM, id="3", audiodownload_allowed=False),
        dict(JM, id="4", audiodownload=""),
    ]}
    src = _src(JamendoSource("cid"), monkeypatch, payload, seen=seen)
    [item] = src.search("sunny", limit=5)
    assert item.asset_id() == "jamendo:777" and item.title == "Sunny Day"
    assert item.author == "Bob" and item.author_url == "https://www.jamendo.com/artist/9"
    assert item.duration == 140.0 and item.media_url == "https://cdn.jamendo/777.mp3"
    assert item.license_name == "CC BY 3.0" and item.license_kind == "free-attribution"
    assert item.tags == ["pop", "guitar", "happy"]
    params = seen[0][1]
    assert params["client_id"] == "cid" and params["audiodownload_allowed"] == "true"


def test_jamendo_api_error_header(monkeypatch):
    src = _src(JamendoSource("cid"), monkeypatch,
               {"headers": {"status": "failed", "error_message": "invalid client_id"}, "results": []})
    with pytest.raises(SourceError, match="invalid client_id"):
        src.search("x")


def test_jamendo_connection_error_never_leaks_client_id(monkeypatch):
    import requests

    src = JamendoSource("SECRET-CID")

    def boom(url, params=None, timeout=None):
        raise requests.ConnectionError(f"Max retries exceeded: {url}?client_id=SECRET-CID")

    monkeypatch.setattr(src.session, "get", boom)
    with pytest.raises(SourceError) as info:
        src.search("x")
    assert "SECRET-CID" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_get_sources_music_works_without_jamendo_key(monkeypatch):
    monkeypatch.delenv("JAMENDO_CLIENT_ID", raising=False)
    monkeypatch.delenv("VIDGRAB_JAMENDO_KEY", raising=False)
    assert [s.name for s in get_sources("music")] == ["openverse"]
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "cid")
    assert [s.name for s in get_sources("music")] == ["openverse", "jamendo"]


def test_music_items_skip_video_filters_and_orientation():
    track = Item(platform="openverse", vid="1", url="u", title="t", duration=95.0,
                 license_kind="free")
    out = tasks._apply_filters([track], fresh_days=0, min_duration=10, max_duration=0,
                               min_views=100, only_free=True, drop_reposts=False,
                               orientation="portrait")
    assert out == [track]

    class O:
        name = "openverse"

    assert tasks._source_kwargs(O(), 10, 0, "portrait") == {"orientation": "portrait"}


# ---------------------------------------------------------------- 音訊下載


@pytest.fixture
def audio_server(tmp_path):
    if not media.ffmpeg_ok():
        pytest.skip("需要 ffmpeg")
    root = tmp_path / "www"
    root.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=6",
                    str(root / "song.mp3")], check=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _track(base, name="song.mp3"):
    return Item(platform="jamendo", vid="55", url="https://www.jamendo.com/track/55", title="t",
                author="Bob", duration=6.0, media_url=f"{base}/{name}", license_kind="free")


@needs_ffmpeg
def test_music_full_download_keeps_original_format(audio_server, tmp_path):
    out = tmp_path / "clips"
    info = media.grab_full(_track(audio_server), out)
    assert Path(info["path"]).name == "jamendo_55_full.mp3"
    assert info["has_audio"] and not info["has_video"] and abs(info["duration"] - 6.0) < 0.3
    assert [p.name for p in out.iterdir()] == ["jamendo_55_full.mp3"]


@needs_ffmpeg
def test_music_clip_is_cut_to_m4a(audio_server, tmp_path):
    out = tmp_path / "clips"
    info = media.grab_clip(_track(audio_server), 1.0, 4.0, out)
    assert Path(info["path"]).suffix == ".m4a"
    assert info["has_audio"] and abs(info["duration"] - 3.0) < 0.3
    assert [p.suffix for p in out.iterdir()] == [".m4a"]


@needs_ffmpeg
def test_music_download_works_even_with_audio_only_flag(audio_server, tmp_path):
    info = media.grab_full(_track(audio_server), tmp_path / "c", media.GrabOptions(audio_only=True))
    assert info["has_audio"]


def test_video_stock_still_rejects_audio_only(tmp_path):
    item = Item(platform="pexels", vid="1", url="u", title="t", media_url="http://x/y.mp4")
    with pytest.raises(media.MediaError, match="音訊"):
        media.grab_full(item, tmp_path, media.GrabOptions(audio_only=True))


@needs_ffmpeg
def test_audio_only_download_is_not_deleted_as_a_broken_video(tmp_path, monkeypatch):
    """回歸：yt-dlp 只要聲音的下載沒有影像軌，不能被「沒有影像軌」那條判斷當壞檔刪掉。"""
    out = tmp_path / "clips"
    item = Item(platform="youtube", vid="abc", url="https://youtu.be/abc", title="t")

    def fake_stream(cmd, on_progress=None, timeout=0, check=None):
        target = cmd[cmd.index("-o") + 1].replace("%(ext)s", "m4a")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=3",
                        "-c:a", "aac", target], check=True)
        return 0, ""

    monkeypatch.setattr(media, "_run_stream", fake_stream)
    monkeypatch.setattr(media, "ytdlp_bin", lambda: "yt-dlp")
    info = media.grab_full(item, out, media.GrabOptions(audio_only=True))
    assert Path(info["path"]).exists() and info["has_audio"]


@needs_ffmpeg
def test_video_download_without_video_stream_is_still_rejected(tmp_path, monkeypatch):
    out = tmp_path / "clips"
    item = Item(platform="youtube", vid="abc", url="https://youtu.be/abc", title="t")

    def fake_stream(cmd, on_progress=None, timeout=0, check=None):
        target = cmd[cmd.index("-o") + 1].replace("%(ext)s", "mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=3",
                        "-c:a", "aac", target], check=True)
        return 0, ""

    monkeypatch.setattr(media, "_run_stream", fake_stream)
    monkeypatch.setattr(media, "ytdlp_bin", lambda: "yt-dlp")
    with pytest.raises(media.MediaError, match="影像軌"):
        media.grab_full(item, out)
