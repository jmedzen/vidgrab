"""素材站（Pexels）來源：搜尋解析、挑檔、直連下載與切片、篩選豁免。全部離線。"""

from __future__ import annotations

import functools
import http.server
import subprocess
import threading
from pathlib import Path

import pytest

from vidgrab import media, tasks
from vidgrab.models import Item
from vidgrab.sources import SourceError, get_source, source_for
from vidgrab.stock import PexelsSource, api_key, pick_file

needs_ffmpeg = pytest.mark.skipif(not media.ffmpeg_ok(), reason="需要 ffmpeg")

FILES = [
    {"id": 1, "quality": "sd", "file_type": "video/mp4", "width": 640, "height": 360, "fps": 25, "link": "https://v/sd.mp4"},
    {"id": 2, "quality": "hd", "file_type": "video/mp4", "width": 1280, "height": 720, "fps": 25, "link": "https://v/hd.mp4"},
    {"id": 3, "quality": "hd", "file_type": "video/mp4", "width": 1920, "height": 1080, "fps": 30, "link": "https://v/fhd.mp4"},
    {"id": 4, "quality": "uhd", "file_type": "video/mp4", "width": 3840, "height": 2160, "fps": 30, "link": "https://v/uhd.mp4"},
    {"id": 5, "quality": "hls", "file_type": "application/x-mpegURL", "width": 0, "height": 0, "link": "https://v/x.m3u8"},
]

RAW = {
    "id": 3195394,
    "url": "https://www.pexels.com/video/aerial-view-of-a-city-at-night-3195394/",
    "image": "https://images.pexels.com/videos/3195394/pic.jpg",
    "duration": 24,
    "user": {"id": 99, "name": "Jane Doe", "url": "https://www.pexels.com/@jane"},
    "video_files": FILES,
}


def test_pick_file_prefers_largest_within_height():
    assert pick_file(FILES, 1080)["link"] == "https://v/fhd.mp4"
    assert pick_file(FILES, 720)["link"] == "https://v/hd.mp4"


def test_pick_file_portrait_uses_short_side():
    portrait = [{"file_type": "video/mp4", "width": 1080, "height": 1920, "link": "https://v/p.mp4"},
                {"file_type": "video/mp4", "width": 2160, "height": 3840, "link": "https://v/p4k.mp4"}]
    assert pick_file(portrait, 1080)["link"] == "https://v/p.mp4"


def test_pick_file_falls_back_to_smallest_and_handles_empty():
    assert pick_file(FILES, 100)["link"] == "https://v/sd.mp4"
    assert pick_file([], 1080) is None
    assert pick_file([FILES[4]], 1080) is None


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("VIDGRAB_PEXELS_KEY", raising=False)
    with pytest.raises(SourceError, match="PEXELS_API_KEY"):
        get_source("pexels")
    assert source_for("pexels") is None


def test_key_from_env_then_file(monkeypatch, tmp_path):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("VIDGRAB_PEXELS_KEY", raising=False)
    assert api_key() == ""
    from vidgrab import auth

    (auth.home() / "pexels.key").write_text("filekey\n", encoding="utf-8")
    assert api_key() == "filekey"
    monkeypatch.setenv("PEXELS_API_KEY", "envkey")
    assert api_key() == "envkey"


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _source(monkeypatch, payload, status=200, seen=None):
    src = PexelsSource(key="k")

    def fake_get(url, params=None, timeout=None):
        if seen is not None:
            seen.append((url, params))
        return _Resp(payload, status)

    monkeypatch.setattr(src.session, "get", fake_get)
    return src


def test_search_maps_fields_and_license(monkeypatch):
    seen: list = []
    src = _source(monkeypatch, {"videos": [RAW]}, seen=seen)
    [item] = src.search("city night", limit=5)
    assert item.asset_id() == "pexels:3195394"
    assert item.title == "Aerial view of a city at night"
    assert item.author == "Jane Doe" and item.author_url == "https://www.pexels.com/@jane"
    assert item.duration == 24.0
    assert item.media_url == "https://v/fhd.mp4"
    assert (item.width, item.height, item.fps) == (1920, 1080, 30.0)
    assert item.license_kind == "free" and "Pexels" in item.license_label
    assert item.is_stock()
    assert "Jane Doe" in __import__("vidgrab.models", fromlist=["x"]).contact_block(item)["credit_hint"]
    assert seen[0][1]["query"] == "city night"
    assert "locale" not in seen[0][1]


def test_search_params_for_cjk_and_orientation(monkeypatch):
    seen: list = []
    src = _source(monkeypatch, {"videos": []}, seen=seen)
    src.search("台北 夜景", limit=3, orientation="portrait")
    params = seen[0][1]
    assert params["locale"] == "zh-TW" and params["orientation"] == "portrait"
    assert params["per_page"] == 6


def test_search_skips_videos_without_usable_file(monkeypatch):
    bad = dict(RAW, id=1, video_files=[FILES[4]])
    src = _source(monkeypatch, {"videos": [bad, RAW]})
    assert [i.vid for i in src.search("x")] == ["3195394"]


@pytest.mark.parametrize("status,needle", [(401, "API key"), (429, "限流"), (503, "暫時不可用")])
def test_search_http_errors_are_source_errors(monkeypatch, status, needle):
    src = _source(monkeypatch, {}, status=status)
    with pytest.raises(SourceError, match=needle):
        src.search("x")


def test_detail_refreshes_media_url(monkeypatch):
    src = _source(monkeypatch, RAW)
    item = Item(platform="pexels", vid="3195394", url="u", title="t", media_url="https://old")
    src.detail(item)
    assert item.media_url == "https://v/fhd.mp4" and not item.error


def test_stock_items_skip_long_video_filters():
    stock = Item(platform="pexels", vid="1", url="u", title="t", duration=20.0, license_kind="free")
    long_ = Item(platform="youtube", vid="2", url="u", title="t", duration=20.0)
    out = tasks._apply_filters([stock, long_], min_duration=10, max_duration=0,
                               min_views=100, only_free=False, drop_reposts=False,
                               fresh_days=0)
    assert [i.vid for i in out] == ["1"]


def test_source_kwargs_only_for_non_stock():
    class S:
        name = "pexels"

    class Y:
        name = "youtube"

    assert tasks._source_kwargs(S(), 10, 0) == {}
    assert tasks._source_kwargs(Y(), 10, 0) == {"min_seconds": 600, "max_seconds": 0}


# ---------------------------------------------------------------- 直連下載


@pytest.fixture
def media_server(tmp_path):
    if not media.ffmpeg_ok():
        pytest.skip("需要 ffmpeg")
    root = tmp_path / "www"
    root.mkdir()
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         "testsrc=duration=6:size=320x240:rate=15", "-pix_fmt", "yuv420p",
         str(root / "clip.mp4")], check=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _item(base: str, name: str = "clip.mp4") -> Item:
    return Item(platform="pexels", vid="42", url="https://www.pexels.com/video/x-42/",
                title="x", author="Jane", duration=6.0, media_url=f"{base}/{name}",
                license_kind="free")


@needs_ffmpeg
def test_grab_full_downloads_direct_file(media_server, tmp_path):
    out = tmp_path / "clips"
    seen: list = []
    info = media.grab_full(_item(media_server), out, on_progress=seen.append)
    assert Path(info["path"]).name == "pexels_42_full.mp4"
    assert info["cached"] is False and info["width"] == 320
    assert seen and seen[-1]["percent"] in (None, 100.0)
    # 暫存目錄與 .part 都要清乾淨
    assert [p.name for p in out.iterdir()] == ["pexels_42_full.mp4"]
    # 第二次直接用快取
    again = media.grab_full(_item(media_server), out)
    assert again["cached"] is True


@needs_ffmpeg
def test_grab_clip_cuts_exact_range(media_server, tmp_path):
    out = tmp_path / "clips"
    info = media.grab_clip(_item(media_server), 1.0, 3.5, out)
    assert Path(info["path"]).name.startswith("pexels_42_")
    assert abs(float(info["duration"]) - 2.5) < 0.2
    assert [p.suffix for p in out.iterdir()] == [".mp4"]


@needs_ffmpeg
def test_grab_direct_404_is_not_retried(media_server, tmp_path, monkeypatch):
    monkeypatch.setattr(media.time, "sleep", lambda s: pytest.fail("不該重試 404"))
    with pytest.raises(media.MediaError, match="404"):
        media.grab_full(_item(media_server, "nope.mp4"), tmp_path / "clips")
    assert list((tmp_path / "clips").iterdir()) == []


def test_grab_direct_rejects_audio_only(tmp_path):
    item = Item(platform="pexels", vid="1", url="u", title="t", media_url="http://x/y.mp4")
    with pytest.raises(media.MediaError, match="音訊"):
        media.grab_full(item, tmp_path, media.GrabOptions(audio_only=True))


@needs_ffmpeg
def test_probe_frames_uses_direct_url(tmp_path):
    src = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=4:size=320x240:rate=15", "-pix_fmt", "yuv420p", str(src)],
                   check=True)
    item = Item(platform="pexels", vid="7", url="u", title="t", duration=4.0, media_url=str(src))
    frames = media.probe_frames(item, tmp_path / "frames", n=2)
    assert len(frames) == 2 and all(f.exists() for f in frames)


def test_item_roundtrip_keeps_media_url():
    item = Item(platform="pexels", vid="1", url="u", title="t", media_url="https://v/a.mp4")
    assert Item(**item.to_dict()).media_url == "https://v/a.mp4"
    # 舊的 results.json 沒有這個欄位也要讀得進來
    legacy = {k: v for k, v in item.to_dict().items() if k != "media_url"}
    assert Item(**legacy).media_url == ""




# ---------------------------------------------------------------- Pixabay / orientation

from vidgrab.stock import (  # noqa: E402
    PixabaySource,
    orientation_ok,
    orientation_of,
    pick_pixabay_file,
    pixabay_key,
)

PIX_HIT = {
    "id": 125,
    "pageURL": "https://pixabay.com/videos/flower-bloom-125/",
    "tags": "flower, bloom, nature, spring, extra",
    "duration": 12,
    "views": 1000,
    "user_id": 77,
    "user": "bob",
    "videos": {
        "large": {"url": "https://cdn/large.mp4", "width": 1920, "height": 1080, "size": 9000000, "thumbnail": "https://cdn/l.jpg"},
        "medium": {"url": "https://cdn/medium.mp4", "width": 1280, "height": 720, "size": 4000000, "thumbnail": "https://cdn/m.jpg"},
        "small": {"url": "https://cdn/small.mp4", "width": 960, "height": 540, "size": 2000000, "thumbnail": "https://cdn/s.jpg"},
        "tiny": {"url": "", "width": 0, "height": 0, "size": 0, "thumbnail": ""},
    },
}
PIX_PORTRAIT = dict(
    PIX_HIT, id=126,
    videos={"large": {"url": "https://cdn/p.mp4", "width": 1080, "height": 1920, "size": 1, "thumbnail": ""}},
)


def _pix(monkeypatch, payload, status=200, seen=None):
    src = PixabaySource(key="k")

    class R(_Resp):
        text = "bad"

    def fake_get(url, params=None, timeout=None):
        if seen is not None:
            seen.append((url, params))
        return R(payload, status)

    monkeypatch.setattr(src.session, "get", fake_get)
    return src


def test_orientation_helpers():
    assert orientation_of(1080, 1920) == "portrait"
    assert orientation_of(1920, 1080) == "landscape"
    assert orientation_of(1000, 1020) == "square"
    assert orientation_of(None, 5) == ""
    it = Item(platform="pexels", vid="1", url="u", title="t", width=1080, height=1920)
    assert orientation_ok(it, "portrait") and not orientation_ok(it, "landscape")
    assert orientation_ok(it, "any") and orientation_ok(it, "")
    unknown = Item(platform="pexels", vid="2", url="u", title="t")
    assert orientation_ok(unknown, "portrait")  # 尺寸不明就保留


def test_pick_pixabay_file_skips_empty_and_respects_height():
    assert pick_pixabay_file(PIX_HIT["videos"], 1080)["url"] == "https://cdn/large.mp4"
    assert pick_pixabay_file(PIX_HIT["videos"], 720)["url"] == "https://cdn/medium.mp4"
    assert pick_pixabay_file(PIX_HIT["videos"], 100)["url"] == "https://cdn/small.mp4"
    assert pick_pixabay_file({"tiny": {"url": ""}}, 1080) is None


def test_pixabay_missing_key(monkeypatch):
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    monkeypatch.delenv("VIDGRAB_PIXABAY_KEY", raising=False)
    assert pixabay_key() == ""
    with pytest.raises(SourceError, match="PIXABAY_API_KEY"):
        get_source("pixabay")


def test_pixabay_search_maps_fields(monkeypatch):
    seen: list = []
    src = _pix(monkeypatch, {"hits": [PIX_HIT]}, seen=seen)
    [item] = src.search("flower", limit=3)
    assert item.asset_id() == "pixabay:125"
    assert item.title == "flower, bloom, nature, spring"
    assert item.tags[:2] == ["flower", "bloom"]
    assert item.author == "bob" and item.author_url == "https://pixabay.com/users/bob-77/"
    assert item.media_url == "https://cdn/large.mp4" and item.duration == 12.0
    assert (item.width, item.height) == (1920, 1080)
    assert item.license_kind == "free" and "Pixabay" in item.license_label
    assert item.is_stock()
    url, params = seen[0]
    assert params["key"] == "k" and params["q"] == "flower"


def test_pixabay_orientation_filters_locally(monkeypatch):
    seen: list = []
    src = _pix(monkeypatch, {"hits": [PIX_HIT, PIX_PORTRAIT]}, seen=seen)
    assert [i.vid for i in src.search("x", limit=5, orientation="portrait")] == ["126"]
    assert [i.vid for i in src.search("x", limit=5, orientation="landscape")] == ["125"]
    assert len(src.search("x", limit=5)) == 2
    assert seen[0][1]["per_page"] == 15  # 要篩方向時多要一些


def test_pixabay_cjk_sets_lang_and_clamps_query(monkeypatch):
    seen: list = []
    src = _pix(monkeypatch, {"hits": []}, seen=seen)
    src.search("台北" * 80, limit=2)
    params = seen[0][1]
    assert params["lang"] == "zh" and len(params["q"]) == 100


@pytest.mark.parametrize("status,needle", [(403, "API key"), (429, "限流"), (503, "暫時不可用"), (400, "拒絕這個查詢")])
def test_pixabay_http_errors(monkeypatch, status, needle):
    src = _pix(monkeypatch, {}, status=status)
    with pytest.raises(SourceError, match=needle):
        src.search("x")


def test_pixabay_detail_refreshes_url(monkeypatch):
    src = _pix(monkeypatch, {"hits": [PIX_HIT]})
    item = Item(platform="pixabay", vid="125", url="u", title="t", media_url="https://old")
    src.detail(item)
    assert item.media_url == "https://cdn/large.mp4" and not item.error
    gone = _pix(monkeypatch, {"hits": []})
    gone.detail(item)
    assert "不存在" in item.error


def test_get_sources_stock_uses_whichever_has_a_key(monkeypatch):
    from vidgrab.sources import get_sources

    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("VIDGRAB_PEXELS_KEY", raising=False)
    monkeypatch.setenv("PIXABAY_API_KEY", "pk")
    assert [s.name for s in get_sources("stock")] == ["pixabay"]
    monkeypatch.setenv("PEXELS_API_KEY", "xk")
    assert [s.name for s in get_sources("stock")] == ["pexels", "pixabay"]
    monkeypatch.delenv("PEXELS_API_KEY")
    monkeypatch.delenv("PIXABAY_API_KEY")
    with pytest.raises(SourceError, match="素材站都不能用"):
        get_sources("stock")


def test_get_source_rejects_group_names():
    with pytest.raises(ValueError, match="get_sources"):
        get_source("stock")


def test_apply_filters_orientation_only_touches_stock():
    port = Item(platform="pixabay", vid="1", url="u", title="a", width=1080, height=1920)
    land = Item(platform="pixabay", vid="2", url="u", title="b", width=1920, height=1080)
    yt = Item(platform="youtube", vid="3", url="u", title="c", duration=1200.0)
    out = tasks._apply_filters([port, land, yt], fresh_days=0, min_duration=0, max_duration=0,
                               min_views=0, only_free=False, drop_reposts=False,
                               orientation="portrait")
    assert [i.vid for i in out] == ["1", "3"]


def test_source_kwargs_orientation_for_stock():
    class P:
        name = "pixabay"

    assert tasks._source_kwargs(P(), 10, 0, "portrait") == {"orientation": "portrait"}
    assert tasks._source_kwargs(P(), 10, 0, "any") == {}


def test_run_search_end_to_end_with_fake_stock(monkeypatch, tmp_path):
    src = _pix(monkeypatch, {"hits": [PIX_HIT, PIX_PORTRAIT]})
    monkeypatch.setattr(tasks, "get_sources", lambda platform: [src])
    res = tasks.run_search(["flower"], platform="pixabay", orientation="portrait",
                           min_duration=10, name="t", emit=lambda m: None)
    assert [i.vid for i in res["items"]] == ["126"]
    manifest, items = tasks.load_project(res["project"])
    assert manifest["sources"] == ["pixabay"] and items[0].media_url == "https://cdn/p.mp4"


def test_run_search_rejects_bad_orientation():
    from vidgrab.models import BadInput

    with pytest.raises(BadInput):
        tasks.run_search(["x"], platform="pixabay", orientation="diagonal")


def test_make_search_job_passes_orientation(monkeypatch):
    from vidgrab import web

    seen: dict = {}

    def fake_run_search(queries, emit=None, **opts):
        seen.update(opts)
        from pathlib import Path

        return {"project": Path("p"), "items": []}

    monkeypatch.setattr(web.tasks, "run_search", fake_run_search)
    job = web.make_search_job({"keywords": ["x"], "platform": "stock", "orientation": "portrait"})
    assert web.wait_job(job, 5)
    assert seen["orientation"] == "portrait" and seen["platform"] == "stock"


def test_pixabay_connection_error_never_leaks_key(monkeypatch):
    import requests

    src = PixabaySource(key="SECRET-KEY-123")

    def boom(url, params=None, timeout=None):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}?key=SECRET-KEY-123")

    monkeypatch.setattr(src.session, "get", boom)
    with pytest.raises(SourceError) as info:
        src.search("x")
    assert "SECRET-KEY-123" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
