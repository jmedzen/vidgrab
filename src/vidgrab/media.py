"""下載與切片：只抓需要的秒數，不把整支影片搬下來。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Item, fmt_ts, log


class MediaError(RuntimeError):
    pass


def ytdlp_bin() -> str:
    env = os.environ.get("VIDGRAB_YTDLP")
    if env and Path(env).exists():
        return env
    bundled = Path(sys.executable).parent / "yt-dlp"
    if bundled.exists():
        return str(bundled)
    found = shutil.which("yt-dlp")
    if found:
        return found
    raise MediaError("找不到 yt-dlp。請重跑 ./vidgrab（會自動安裝）或 pip install yt-dlp")


def ffmpeg_ok() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(cmd: list[str], timeout: float = 900) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"逾時（>{int(timeout)}s）：{' '.join(cmd[:4])} …") from exc


@dataclass
class GrabOptions:
    height: int = 1080
    audio_only: bool = False
    audio_format: str = "m4a"
    cookies: str | None = None
    cookies_from_browser: str | None = None
    extra_format: str | None = None


def _common_args(opts: GrabOptions) -> list[str]:
    args = [
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--retries",
        "5",
        "--fragment-retries",
        "5",
        "--socket-timeout",
        "30",
        "--concurrent-fragments",
        "4",
    ]
    if opts.cookies:
        args += ["--cookies", opts.cookies]
    if opts.cookies_from_browser:
        args += ["--cookies-from-browser", opts.cookies_from_browser]
    return args


def _format_selector(opts: GrabOptions) -> str:
    if opts.extra_format:
        return opts.extra_format
    h = opts.height
    if opts.audio_only:
        return "ba/b"
    return f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"


def probe_video(url: str, opts: GrabOptions | None = None, timeout: float = 300) -> dict[str, Any]:
    """抓單支影片的完整資訊（授權、可用格式、是否需登入）。"""
    opts = opts or GrabOptions()
    cmd = [ytdlp_bin(), "-J", "--no-playlist", "--no-warnings"] + _common_args(opts) + [url]
    proc = _run(cmd, timeout=timeout)
    if proc.returncode != 0:
        raise MediaError((proc.stderr or proc.stdout or "").strip().splitlines()[-1] if (proc.stderr or proc.stdout) else "yt-dlp 失敗")
    return json.loads(proc.stdout)


# ---------------------------------------------------------------- 抓畫面


def probe_frames(item: Item, outdir: Path, n: int = 3, opts: GrabOptions | None = None,
                 points: list[float] | None = None, width: int = 480) -> list[Path]:
    """從影片裡抽 n 格畫面（只在需要的時間點各下載 1 秒，成本極低）。"""
    opts = opts or GrabOptions(height=480)
    outdir.mkdir(parents=True, exist_ok=True)
    if not points:
        dur = item.duration or 0
        if dur <= 0:
            points = [0.0]
        else:
            step = dur / (n + 1)
            points = [max(0.0, step * (i + 1)) for i in range(n)]

    frames: list[Path] = []
    for idx, point in enumerate(points):
        raw = outdir / f"{item.platform}_{item.vid}_{idx:02d}.mp4"
        frame = outdir / f"{item.platform}_{item.vid}_{idx:02d}.jpg"
        if frame.exists():
            frames.append(frame)
            continue
        cmd = (
            [ytdlp_bin()]
            + _common_args(GrabOptions(height=480, cookies=opts.cookies,
                                       cookies_from_browser=opts.cookies_from_browser))
            + [
                "-f",
                "bv*[height<=480]/b[height<=480]/bv*/b",
                "--download-sections",
                f"*{point:.1f}-{point + 1.2:.1f}",
                "-o",
                str(raw.with_suffix(".%(ext)s")),
                item.url,
            ]
        )
        proc = _run(cmd, timeout=180)
        got = sorted(outdir.glob(f"{item.platform}_{item.vid}_{idx:02d}.*"))
        got = [p for p in got if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".flv", ".m4v")]
        if proc.returncode != 0 or not got:
            continue
        proc2 = _run(
            [
                "ffmpeg", "-v", "error", "-y", "-ss", "0.2", "-i", str(got[0]),
                "-frames:v", "1", "-vf", f"scale={width}:-2", str(frame),
            ],
            timeout=60,
        )
        for p in got:
            p.unlink(missing_ok=True)
        if proc2.returncode == 0 and frame.exists():
            frames.append(frame)
    return frames


def contact_sheet(frames: list[Path], dest: Path, height: int = 270) -> bool:
    """幾格畫面和成一張橫排圖，一眼看完整段素材。"""
    frames = [f for f in frames if Path(f).exists()]
    if not frames:
        return False
    if len(frames) == 1:
        shutil.copy(frames[0], dest)
        return True
    args = ["ffmpeg", "-v", "error", "-y"]
    for f in frames:
        args += ["-i", str(f)]
    chains = []
    for i in range(len(frames)):
        chains.append(
            f"[{i}:v]scale=480:{height}:force_original_aspect_ratio=decrease,"
            f"pad=480:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]"
        )
    stack = "".join(f"[v{i}]" for i in range(len(frames))) + f"hstack=inputs={len(frames)}"
    args += ["-filter_complex", ";".join(chains + [stack]), "-frames:v", "1", str(dest)]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    return proc.returncode == 0 and dest.exists()


# ---------------------------------------------------------------- 切片


def grab_clip(
    item: Item,
    start: float,
    end: float,
    outdir: Path,
    opts: GrabOptions | None = None,
    label: str = "",
    timeout: float = 900,
) -> dict[str, Any]:
    """下載 start→end 這一段，回傳檔案資訊。"""
    opts = opts or GrabOptions()
    outdir.mkdir(parents=True, exist_ok=True)
    stem = f"{item.platform}_{item.vid}_{fmt_ts(start).replace(':', '')}-{fmt_ts(end).replace(':', '')}"
    if label:
        stem += f"_{label}"
    target = outdir / f"{stem}.{'m4a' if opts.audio_only else 'mp4'}"
    if target.exists() and target.stat().st_size > 0:
        info = ffprobe(target)
        return {"path": str(target), "cached": True, **info}

    if end <= start:
        raise MediaError(f"片段長度必須大於 0（{start} → {end}）")

    cmd = [ytdlp_bin()] + _common_args(opts) + [
        "-f", _format_selector(opts),
        "--download-sections", f"*{start:.2f}-{end:.2f}",
    ]
    if not opts.audio_only:
        cmd += ["--force-keyframes-at-cuts", "--merge-output-format", "mp4"]
    else:
        cmd += ["-x", "--audio-format", opts.audio_format]
    cmd += ["-o", str(outdir / f"{stem}.%(ext)s"), item.url]

    log(f"    ↓ {item.platform} {item.vid} {fmt_ts(start)}→{fmt_ts(end)}")
    proc = _run(cmd, timeout=timeout)
    produced = sorted(
        p for p in outdir.glob(f"{stem}.*")
        if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".opus", ".flac", ".wav")
    )
    if proc.returncode != 0 or not produced:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise MediaError(tail[-1] if tail else "yt-dlp 沒有產出檔案")
    final = produced[0]
    sized = ffprobe(final)
    return {"path": str(final), "cached": False, **sized}


def ffprobe(path: Path) -> dict[str, Any]:
    proc = _run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        timeout=60,
    )
    if proc.returncode != 0:
        return {}
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float((data.get("format") or {}).get("duration") or (video or {}).get("duration") or 0)
    return {
        "duration": round(duration, 2),
        "width": int(video["width"]) if video and video.get("width") else None,
        "height": int(video["height"]) if video and video.get("height") else None,
        "vcodec": (video or {}).get("codec_name"),
        "acodec": (audio or {}).get("codec_name"),
        "has_audio": audio is not None,
        "has_video": video is not None,
        "size_bytes": Path(path).stat().st_size if Path(path).exists() else 0,
    }


def grab_thumb(url: str, dest: Path) -> bool:
    """下載封面圖，失敗就算了。"""
    import requests

    try:
        resp = requests.get(
            url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
        )
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return True
    except Exception:  # noqa: BLE001
        return False
