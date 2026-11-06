#!/usr/bin/env python3
"""造一份測試用素材庫，讓 check_ui.py 不依賴真人的資料。

    python tools/make_fixture.py /tmp/vg-ui-fixture

會做出一個專案，裡面有 2 支候選、其中一支有 1 個片段（含縮圖），
外加一段真的能播放的短影片（用 ffmpeg 生，沒有就用空檔佔位）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _ffmpeg() -> str | None:
    for cand in ("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/bin/ffmpeg"):
        try:
            subprocess.run([cand, "-version"], capture_output=True, timeout=10, check=True)
            return cand
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _make_clip(path: Path) -> bool:
    """生一段 2 秒的測試影片；沒有 ffmpeg 就寫個佔位檔。"""
    ff = _ffmpeg()
    path.parent.mkdir(parents=True, exist_ok=True)
    if ff:
        cmd = [
            ff, "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=15:duration=2",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60, check=True)
            return path.exists() and path.stat().st_size > 0
        except (OSError, subprocess.SubprocessError):
            pass
    path.write_bytes(b"\x00" * 2048)
    return True


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/vg-ui-fixture") / "projects"
    proj = root / "ui-fixture"
    clips = proj / "clips"
    thumbs = proj / "thumbs"
    clips.mkdir(parents=True, exist_ok=True)
    thumbs.mkdir(parents=True, exist_ok=True)

    (proj / "manifest.json").write_text(json.dumps({
        "queries": ["測試素材"],
        "sources": ["bilibili"],
        "created": "2026-01-01T00:00:00",
    }, ensure_ascii=False), encoding="utf-8")

    clip_name = "bilibili_BVUIFIXTURE_000-002_測試.mp4"
    _make_clip(clips / clip_name)
    # 縮圖：1x1 灰點 PNG，足夠讓 <img> 有東西可載
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6360000002000100ffff03000006000557bfabd40000"
        "000049454e44ae426082"
    )
    (thumbs / "clip.png").write_bytes(png)

    # 欄位要跟 models.Item 對得上；duration_sec / views_label 那類是算出來的，
    # 不能寫進 results.json（寫了會 TypeError）。
    items = [
        {
            "platform": "bilibili", "vid": "BVUIFIXTURE",
            "url": "https://www.bilibili.com/video/BVUIFIXTURE",
            "title": "測試素材（有片段）", "author": "測試 UP",
            "keyword": "測試素材", "duration": 2.0,
            "views": 1234, "pubdate": "20260101",
            "license_kind": "free", "license_label": "可自由使用",
            "repost": False,
            # 跟真實資料一致：存絕對路徑。web.py 的 _thumb_url 會用
            # Path(thumb).exists() 驗證，寫成 /file/... 會永遠是空的。
            "thumbs": [str(thumbs / "clip.png")],
            "clips": [{
                "name": clip_name,
                "path": str(clips / clip_name),
                "rel": f"/file/ui-fixture/clips/{clip_name}",
                "start": 0.0, "end": 2.0, "full": False,
                "span": "00:00→00:02", "size": "2.0 KB",
                "dims": "320×180", "orientation": "landscape",
                "thumb": str(thumbs / "clip.png"),
            }],
        },
        {
            "platform": "youtube", "vid": "uiFIXTURE2",
            "url": "https://youtu.be/uiFIXTURE2",
            "title": "測試素材（沒片段）", "author": "測試頻道",
            "keyword": "測試素材", "duration": 30.0,
            "views": 88, "pubdate": "20260102",
            "license_kind": "unknown", "license_label": "",
            "repost": False,
            "thumbs": [], "clips": [],
        },
    ]
    (proj / "results.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✔ 測試素材：{proj}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
