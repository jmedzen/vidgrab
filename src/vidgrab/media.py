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


MEDIA_EXTS = (".mp4", ".mkv", ".webm", ".flv", ".m4v", ".mov",
              ".m4a", ".mp3", ".opus", ".flac", ".wav")

# yt-dlp 的進度輸出格式：分行、好解析，比啃它的人類可讀進度條可靠
PROGRESS_TEMPLATE = (
    "download:VIDGRAB %(progress.downloaded_bytes)s %(progress.total_bytes_estimate)s "
    "%(progress.speed)s %(progress.eta)s %(progress.fragment_index)s %(progress.fragment_count)s"
)


def _fmt_speed(value: float | None) -> str:
    if not value or value <= 0:
        return ""
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.1f}MB/s"
    return f"{value / 1024:.0f}KB/s"


def _fmt_eta(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _parse_progress(line: str) -> dict[str, Any] | None:
    """把 VIDGRAB 進度行變成 dict；不是進度行就回 None。"""
    if not line.startswith("VIDGRAB "):
        return None
    parts = line.split()
    if len(parts) < 7:
        return None

    def num(text: str) -> float | None:
        try:
            value = float(text)
        except ValueError:
            return None
        return value if value >= 0 else None

    done, total, speed, eta = num(parts[1]), num(parts[2]), num(parts[3]), num(parts[4])
    frag_i, frag_n = num(parts[5]), num(parts[6])
    percent = (done / total * 100) if done and total else None
    return {
        "downloaded": done,
        "total": total,
        "percent": round(percent, 1) if percent is not None else None,
        "speed": _fmt_speed(speed),
        "eta": _fmt_eta(eta),
        "fragment": (f"{int(frag_i)}/{int(frag_n)}" if frag_i is not None and frag_n else ""),
    }


def _run_stream(cmd: list[str], on_progress=None, timeout: float = 7200) -> tuple[int, str]:
    """邊跑邊送進度；回傳 (returncode, 最後幾行錯誤)。"""
    import threading

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, errors="replace",
    )
    tail: list[str] = []
    state = {"last_emit": 0.0, "last_pct": -1.0}

    def reader() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if not line:
                continue
            if line.startswith("VIDGRAB ") or line.startswith("[download]") or "[Merger]" in line:
                parsed = _parse_progress(line)
                if parsed and on_progress:
                    import time as _t

                    now = _t.time()
                    pct = parsed.get("percent")
                    # 每 1% 或每 2 秒回報一次，避免洗畫面
                    if pct is None or pct - state["last_pct"] >= 1 or now - state["last_emit"] >= 2:
                        state["last_pct"] = pct if pct is not None else state["last_pct"]
                        state["last_emit"] = now
                        on_progress(parsed)
            tail.append(line)
            del tail[:-40]

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise MediaError(f"逾時（>{int(timeout)}s）")
    thread.join(timeout=5)
    return code, "\n".join(tail[-12:])


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
        # B 站的音訊 CDN 只認 Range 請求，直接 GET 會回 567；
        # 這道設定強迫 yt-dlp 分段抓，是整支下載成功的關鍵
        "--http-chunk-size",
        "10M",
        "--newline",
        "--progress-template",
        PROGRESS_TEMPLATE,
        # 下載當下就把完整 metadata 寫成 sidecar json。
        # 這是素材庫的資料來源：以後要查標籤、作者、授權都靠它，
        # 不必再回頭爬一次（影片可能已下架或改版）。
        "--write-info-json",
        "--no-clean-info-json",
    ]
    if opts.cookies:
        args += ["--cookies", opts.cookies]
    if opts.cookies_from_browser:
        args += ["--cookies-from-browser", opts.cookies_from_browser]
    return args


def _format_selector(opts: GrabOptions) -> str:
    if opts.extra_format:
        return opts.extra_format
    return "ba/b" if opts.audio_only else "bv*+ba/b"


def _sort_args(opts: GrabOptions) -> list[str]:
    """用 -S 讓 yt-dlp 自己挑解析度：直式 1080x1920 和橫式 1920x1080 都對得上。"""
    if opts.extra_format or opts.audio_only:
        return []
    return ["-S", f"res:{opts.height},fps"]


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
                "bv*+ba/b",
                "-S",
                "res:480,fps",
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
    timeout: float = 7200,
    on_progress=None,
) -> dict[str, Any]:
    """下載 start→end 這一段。"""
    opts = opts or GrabOptions()
    if end <= start:
        raise MediaError(f"片段長度必須大於 0（{start} → {end}）")
    return grab_range(item, start, end, outdir, opts, label=label,
                      timeout=timeout, on_progress=on_progress)


def grab_full(
    item: Item,
    outdir: Path,
    opts: GrabOptions | None = None,
    label: str = "",
    timeout: float = 14400,
    on_progress=None,
) -> dict[str, Any]:
    """整支影片下載（不切片）。"""
    opts = opts or GrabOptions()
    return grab_range(item, None, None, outdir, opts, label=label,
                      timeout=timeout, on_progress=on_progress)


def grab_range(
    item: Item,
    start: float | None,
    end: float | None,
    outdir: Path,
    opts: GrabOptions | None = None,
    label: str = "",
    timeout: float = 7200,
    on_progress=None,
) -> dict[str, Any]:
    """核心：start/end 都給 None = 整支，否則只抓那一段。"""
    opts = opts or GrabOptions()
    outdir.mkdir(parents=True, exist_ok=True)

    if start is None:
        stem = f"{item.platform}_{item.vid}_full"
    else:
        stem = f"{item.platform}_{item.vid}_{fmt_ts(start).replace(':', '')}-{fmt_ts(end or 0).replace(':', '')}"
    if label:
        stem += f"_{label}"

    # 已經抓過就直接回報
    for ext in MEDIA_EXTS:
        existing = outdir / f"{stem}{ext}"
        if existing.exists() and existing.stat().st_size > 0:
            return {"path": str(existing), "cached": True, **ffprobe(existing)}

    cmd = [ytdlp_bin()] + _common_args(opts) + ["-f", _format_selector(opts)] + _sort_args(opts)
    if start is not None:
        cmd += ["--download-sections", f"*{float(start):.2f}-{float(end):.2f}"]
        if not opts.audio_only:
            cmd += ["--force-keyframes-at-cuts"]
    if not opts.audio_only:
        cmd += ["--merge-output-format", "mp4"]
    else:
        cmd += ["-x", "--audio-format", opts.audio_format]
    cmd += ["-o", str(outdir / f"{stem}.%(ext)s"), item.url]

    if start is None:
        log(f"    ↓ {item.platform} {item.vid} 整支")
    else:
        log(f"    ↓ {item.platform} {item.vid} {fmt_ts(start)}→{fmt_ts(end or 0)}")

    code, tail = _run_stream(cmd, on_progress=on_progress, timeout=timeout)
    produced = sorted(
        p for p in outdir.glob(f"{stem}.*") if p.suffix.lower() in MEDIA_EXTS
    )
    if code != 0 or not produced:
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        raise MediaError(lines[-1] if lines else "yt-dlp 沒有產出檔案")
    final = produced[0]
    # 合併音視訊時，yt-dlp 會留下暫存軌；這裡只留成品
    for leftover in outdir.glob(f"{stem}.f*"):
        if leftover != final:
            leftover.unlink(missing_ok=True)

    # 把 sidecar info json 的內容併進 item：這是素材庫的資料來源
    _absorb_info_json(item, outdir, stem)

    probe = ffprobe(final)
    return {"path": str(final), "cached": False, **probe}


def _absorb_info_json(item: Item, outdir: Path, stem: str) -> None:
    """讀回 yt-dlp 寫的 .info.json，補齊 item 的素材庫欄位。

    讀不到不算失敗——下載本身已經成功了，metadata 缺了只是少一點資訊。
    """
    from .models import enrich_from_info

    candidates = sorted(outdir.glob(f"{stem}*.info.json"))
    if not candidates:
        return
    path = candidates[0]
    try:
        info = json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return
    enrich_from_info(item, info)
    item.info_json = str(path)


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
