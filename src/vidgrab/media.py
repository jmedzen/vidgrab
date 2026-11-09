"""下載與切片：只抓需要的秒數，不把整支影片搬下來。"""

from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Item, human_size, fmt_ts, log


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
    """把 VIDGRAB 進度行變成 dict；不是進度行就回 None。

    實測 yt-dlp 送出來的行長這樣（欄位會少）：
        VIDGRAB 7283754 NA
    只有下載位元組數，沒有總量、沒有速度。
    原本硬要求 7 個欄位，所以**一次都沒解析成功**，進度條永遠是空的。

    這裡改成：缺的欄位就當 None，能算出多少算多少。
    """
    marker = "VIDGRAB "
    at = line.find(marker)
    if at < 0:
        return None

    parts = line[at:].split()[1:]  # 去掉 VIDGRAB 本身

    def num(idx: int) -> float | None:
        if idx >= len(parts):
            return None
        text = parts[idx]
        if text in ("NA", "N/A", "None", "-"):
            return None
        try:
            value = float(text)
        except ValueError:
            return None
        return value if value >= 0 else None

    done = num(0)
    total = num(1)
    speed = num(2)
    eta = num(3)
    frag_i = num(4)
    frag_n = num(5)

    # 什麼都沒有就別回報，免得送出一堆空事件
    if done is None and total is None:
        return None

    percent = (done / total * 100) if done and total else None
    return {
        "downloaded": done,
        "total": total,
        "percent": round(min(percent, 100.0), 1) if percent is not None else None,
        "speed": _fmt_speed(speed),
        "eta": _fmt_eta(eta),
        "fragment": (f"{int(frag_i)}/{int(frag_n)}" if frag_i is not None and frag_n else ""),
        "bytes": human_size(done) if done else "",
    }


def _kill_group(proc: subprocess.Popen) -> None:
    """整組殺掉（yt-dlp 會再生 ffmpeg），並 wait() 回收，避免 zombie 與孤兒行程。"""
    import os
    import signal

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def _run_stream(cmd: list[str], on_progress=None, timeout: float = 7200,
                check=None) -> tuple[int, str]:
    """邊跑邊送進度；回傳 (returncode, 最後幾行錯誤)。

    - on_progress 丟例外（例如批次期限到了）時，reader 不會死：它記下例外、繼續把
      stdout 讀完（不然 pipe 滿了 yt-dlp 會卡住），由主執行緒殺掉整個 process group
      後把同一個例外拋出去。
    - check() 回傳例外物件時（heartbeat thread 用），行為同上。
    """
    import threading

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, errors="replace", start_new_session=True,
    )
    tail: list[str] = []
    state = {"last_emit": 0.0, "last_pct": -1.0}
    aborted: list[BaseException] = []

    def reader() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if not line:
                continue
            if (line.startswith("VIDGRAB ") or line.startswith("[download]") or "[Merger]" in line) \
                    and not aborted:
                parsed = _parse_progress(line)
                if parsed and on_progress:
                    import time as _t

                    now = _t.time()
                    pct = parsed.get("percent")
                    # 每 1% 或每 2 秒回報一次，避免洗畫面
                    if pct is None or pct - state["last_pct"] >= 1 or now - state["last_emit"] >= 2:
                        state["last_pct"] = pct if pct is not None else state["last_pct"]
                        state["last_emit"] = now
                        try:
                            on_progress(parsed)
                        except BaseException as exc:  # noqa: BLE001 - 交給主執行緒處理
                            aborted.append(exc)
            tail.append(line)
            del tail[:-40]

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.time() + timeout
    while True:
        try:
            code = proc.wait(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            pass
        if not aborted and check is not None:
            err = check()
            if err is not None:
                aborted.append(err)
        if aborted:
            _kill_group(proc)
            thread.join(timeout=5)
            raise aborted[0]
        if time.time() > deadline:
            _kill_group(proc)
            thread.join(timeout=5)
            raise MediaError(f"逾時（>{int(timeout)}s）")
    thread.join(timeout=5)
    if aborted:
        raise aborted[0]
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
        # 注意：這裡**不能**加 --no-progress。
        # 它會連 --progress-template 的輸出一起壓掉，
        # 導致進度條永遠空白（這個 bug 存在很久，因為沒人驗過進度真的會動）。
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
        if item.media_url:
            # 素材站有直連網址：ffmpeg 直接 seek 到那一秒抽一格，不必先下載
            proc = _run(
                ["ffmpeg", "-v", "error", "-y", "-ss", f"{point:.2f}", "-i", item.media_url,
                 "-frames:v", "1", "-vf", f"scale={width}:-2", str(frame)],
                timeout=120,
            )
            if proc.returncode == 0 and frame.exists() and frame.stat().st_size > 0:
                frames.append(frame)
            else:
                frame.unlink(missing_ok=True)
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


def thumb_from_clip(clip_path: Path, dest: Path, at: float | None = None,
                    width: int = 640) -> Path | None:
    """從**已經下載好的片段**抽一格當縮圖。

    這是唯一能保證「縮圖就是這個片段」的做法。
    原本的做法是另外去整支影片均勻位置抓 3 格——那些畫面對不對得上
    全靠運氣（實測：片段在 30–75 秒，抓的卻是 914/1827/2740 秒）。
    """
    clip_path = Path(clip_path)
    if not clip_path.exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    where = 0.5 if at is None else max(0.0, float(at))
    if at is None:
        # 取片段中間：+-0.2 秒內多半是黑場或轉場，中間最安全
        dur = 0.0
        try:
            dur = float(ffprobe(clip_path).get("duration") or 0)
        except Exception:  # noqa: BLE001
            dur = 0.0
        where = dur / 2 if dur > 0.4 else 0.0

    proc = _run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-ss", f"{where:.2f}", "-i", str(clip_path),
            "-frames:v", "1", "-vf", f"scale={width}:-2", str(dest),
        ],
        timeout=60,
    )
    if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
        return dest
    # 中間抽不到（超短片段）就退回第一格
    if where > 0:
        return thumb_from_clip(clip_path, dest, at=0.0, width=width)
    return None


def extract_frame(video: Path, dest: Path, offset: float, width: int = 640) -> Path | None:
    """從本機影片抽一格：offset 是檔案內的秒數。jpg 用高畫質，png 無損。已存在就直接回傳（快取）。"""
    video, dest = Path(video), Path(dest)
    if not video.exists():
        return None
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    quality = ["-q:v", "2"] if dest.suffix.lower() in (".jpg", ".jpeg") else []
    proc = _run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, offset):.3f}", "-i", str(video),
         "-frames:v", "1", "-vf", f"scale={int(width)}:-2", *quality, str(dest)],
        timeout=60,
    )
    if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.unlink(missing_ok=True)
    return None


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


# 下載失敗最多重試幾次。10 次是使用者要求的；搭配下面的退避，
# 大約會撐 3 分鐘左右，足夠撐過網路的短暫抽動。
# 預設值留在這裡當文件；實際以 config 為準（見 _retries()）。
MAX_DOWNLOAD_RETRIES = 10


def _retries() -> int:
    """下載重試次數：以 config 為準，讀不到才用模組預設。"""
    from . import config

    try:
        return max(1, int(config.get("download_retries", MAX_DOWNLOAD_RETRIES)))
    except Exception:  # noqa: BLE001
        return MAX_DOWNLOAD_RETRIES

# 這些錯誤重試有用（網路抽動、伺服器忙、暫時被限流）。
# 其他錯誤（404、私人影片、地區限制）重試幾次都一樣，直接放棄。
_RETRYABLE = (
    "http error 5",          # 5xx
    "http error 429",        # 太頻繁
    "timed out",
    "timeout",
    "connection",
    "temporarily unavailable",
    "unable to download",
    "read timed out",
    "reset by peer",
    "ssl",
    "fragment",
    "giving up after",
    "incomplete read",
    "network is unreachable",
)


def _is_retryable(message: str) -> bool:
    low = (message or "").lower()
    return any(token in low for token in _RETRYABLE)


def _backoff(attempt: int) -> float:
    """1s, 2s, 4s … 上限由 config 決定（預設 20s）。"""
    from . import config

    try:
        cap = float(config.get("retry_max_wait", 20.0))
    except Exception:  # noqa: BLE001
        cap = 20.0
    return min(max(1.0, cap), float(2 ** max(0, attempt - 1)))


def _download_file(url: str, dest: Path, timeout: float, on_progress=None) -> None:
    """串流下載到 dest（先寫 .part，完成才改名）。

    錯誤訊息刻意用 `_RETRYABLE` 認得的字眼（HTTP error 5xx、timed out、connection），
    讓外層的退避重試只對真的有機會好的失敗重試。
    """
    import requests

    part = dest.with_name(dest.name + ".part")
    deadline = time.time() + timeout
    try:
        with requests.get(url, stream=True, timeout=30,
                          headers={"User-Agent": "vidgrab"}) as resp:
            if resp.status_code >= 500 or resp.status_code == 429:
                raise MediaError(f"HTTP error {resp.status_code}")
            if resp.status_code >= 400:
                raise MediaError(f"HTTP {resp.status_code}：素材網址失效，"
                                 "請先 refresh 取得新網址")
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            began = time.time()
            last = 0.0
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now > deadline:
                        raise MediaError(f"逾時（>{int(timeout)}s）")
                    if on_progress and now - last >= 0.5:
                        last = now
                        speed = done / max(0.001, now - began)
                        on_progress({
                            "downloaded": done, "total": total or None,
                            "percent": round(done / total * 100, 1) if total else None,
                            "speed": _fmt_speed(speed),
                            "eta": _fmt_eta((total - done) / speed) if total and speed else "",
                            "fragment": "", "bytes": human_size(done),
                        })
        if total and done < total:
            raise MediaError("incomplete read：檔案沒下載完整")
        part.replace(dest)
    except requests.RequestException as exc:
        raise MediaError(f"connection error：{type(exc).__name__}") from exc
    finally:
        part.unlink(missing_ok=True)


def _grab_direct(item: Item, start: float | None, end: float | None, outdir: Path,
                 stem: str, opts: GrabOptions, timeout: float, on_progress=None) -> dict[str, Any]:
    """素材站（Pexels 等）：直連下載整支，再用 ffmpeg 切出需要的區段。

    這類影片只有幾十秒、檔案幾 MB，整支抓下來比 yt-dlp 的分段機制更單純可靠。
    切片一律重新編碼（libx264/aac），這樣起點是精準的，不受關鍵幀影響。
    """
    import tempfile

    music = item.is_music()
    if opts.audio_only and not music:
        raise MediaError("這個來源沒有獨立的音訊檔，不支援只要聲音")
    if not ffmpeg_ok():
        raise MediaError("缺 ffmpeg／ffprobe，無法處理素材站檔案")

    # 音樂：整支保留原格式（認得的副檔名），切片一律轉 m4a（AAC，ffmpeg 內建編碼器）
    src_ext = Path(item.media_url.split("?", 1)[0]).suffix.lower()
    if music:
        keep = start is None and src_ext in MEDIA_EXTS
        final = outdir / f"{stem}{src_ext if keep else '.m4a'}"
    else:
        final = outdir / f"{stem}.mp4"
    limit = _retries()
    last_error = ""
    tmpdir = Path(tempfile.mkdtemp(prefix=f".{stem}.", dir=str(outdir)))
    try:
        src = tmpdir / ("source" + (src_ext if music and src_ext else ".mp4"))
        log(f"    ↓ {item.platform} {item.vid} " + ("整支" if start is None
            else f"{fmt_ts(start)}→{fmt_ts(end or 0)}"))
        for attempt in range(1, limit + 1):
            try:
                _download_file(item.media_url, src, timeout, on_progress)
                last_error = ""
                break
            except MediaError as exc:
                last_error = str(exc)
                if attempt >= limit or not _is_retryable(last_error):
                    raise
                wait = _backoff(attempt)
                log(f"    ↻ 第 {attempt}/{limit} 次失敗，{wait:.0f} 秒後重試：{last_error[:90]}")
                time.sleep(wait)

        if start is None and final.suffix == src.suffix:
            shutil.move(str(src), str(final))
        else:
            # 切片，或整支但格式不是素材庫認得的（音樂轉 m4a）
            part = tmpdir / f"cut{final.suffix}"
            cmd = ["ffmpeg", "-v", "error", "-y"]
            if start is not None:
                cmd += ["-ss", f"{float(start):.3f}"]
            cmd += ["-i", str(src)]
            if start is not None:
                cmd += ["-t", f"{max(0.1, float(end) - float(start)):.3f}"]
            if music:
                cmd += ["-vn", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
            else:
                cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
            proc = _run(cmd + [str(part)], timeout=max(60.0, min(timeout, 900.0)))
            if proc.returncode != 0 or not part.exists():
                tail = (proc.stderr or "").strip().splitlines()[-1:] or ["ffmpeg 切片失敗"]
                raise MediaError(tail[0])
            part.replace(final)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    probe = ffprobe(final)
    if music:
        if not probe.get("has_audio"):
            final.unlink(missing_ok=True)
            raise MediaError("下載回來的檔案是壞的（沒有音訊軌），已刪除")
    elif not (probe.get("width") or probe.get("height")):
        final.unlink(missing_ok=True)
        raise MediaError("下載回來的檔案是壞的（沒有影像軌），已刪除")
    return {"path": str(final), "cached": False, **probe}


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

    if item.media_url:
        return _grab_direct(item, start, end, outdir, stem, opts, timeout, on_progress)

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

    # 切片下載（--download-sections）走 ffmpeg 直取，
    # yt-dlp 只在結束時吐一行進度，中間完全是空的。
    # 所以另外掛一個心跳：確實有東西在下載，就讓前端看到「還在動」，
    # 但標成 estimated，不假裝知道真實百分比。
    stop_beat = threading.Event()
    beat_error: list[BaseException] = []
    beater = None
    if start is not None and on_progress:
        span = max(1.0, float(end or start) - float(start))
        began = time.time()
        # 經驗值：實測約 8 倍速，300 秒的片段約 40 秒抓完
        expect = max(6.0, span / 8.0)

        def beat() -> None:
            while not stop_beat.wait(1.0):
                elapsed = time.time() - began
                pct = min(95.0, elapsed / expect * 100)
                try:
                    on_progress({
                        "downloaded": None, "total": None,
                        "percent": round(pct, 1),
                        "speed": "", "eta": "", "fragment": "", "bytes": "",
                        "estimated": True,
                    })
                except BaseException as exc:  # noqa: BLE001
                    # 例如批次期限到了：記下來，讓 _run_stream 殺掉 yt-dlp 並拋出
                    beat_error.append(exc)
                    return

        beater = threading.Thread(target=beat, daemon=True)
        beater.start()

    produced: list[Path] = []
    tail = ""
    last_error = ""
    limit = _retries()
    for attempt in range(1, limit + 1):
        try:
            code, tail = _run_stream(
                cmd, on_progress=on_progress, timeout=timeout,
                check=lambda: beat_error[0] if beat_error else None,
            )
        except BaseException:
            # 逾時、批次期限、Ctrl-C：殘檔一定要清，不然下次會從壞掉的地方續傳
            _clean_partials(outdir, stem)
            raise
        finally:
            stop_beat.set()

        produced = sorted(
            p for p in outdir.glob(f"{stem}.*") if p.suffix.lower() in MEDIA_EXTS
        )
        if code == 0 and produced:
            break

        lines = [ln for ln in tail.splitlines() if ln.strip()]
        last_error = lines[-1] if lines else "yt-dlp 沒有產出檔案"

        # 殘檔一定要清：留著下次會從壞掉的地方續傳，永遠修不好
        _clean_partials(outdir, stem)

        if attempt >= limit or not _is_retryable(last_error):
            raise MediaError(last_error)

        wait = _backoff(attempt)
        log(f"    ↻ 第 {attempt}/{limit} 次失敗，{wait:.0f} 秒後重試：{last_error[:90]}")
        if on_progress:
            try:
                on_progress({
                    "downloaded": None, "total": None, "percent": None,
                    "speed": "", "eta": "", "fragment": "",
                    "bytes": "", "estimated": True,
                    "retry": attempt, "max_retries": limit,
                })
            except Exception:  # noqa: BLE001
                pass
        time.sleep(wait)

    if not produced:
        raise MediaError(last_error or "yt-dlp 沒有產出檔案")
    # yt-dlp 有時會留下 `xxx.f30077.mp4` 這種帶格式編號的檔名。
    # 那是內部識別用的，對使用者與 agent 都是噪音，改成乾淨的名字。
    produced = [_normalize_name(p, stem) for p in produced]
    final = produced[0]
    # 合併音視訊時，yt-dlp 會留下暫存軌；這裡只留成品
    for leftover in outdir.glob(f"{stem}.f*"):
        if leftover != final and leftover.suffix.lower() not in MEDIA_EXTS:
            leftover.unlink(missing_ok=True)

    # 把 sidecar info json 的內容併進 item：這是素材庫的資料來源
    _absorb_info_json(item, outdir, stem)

    probe = ffprobe(final)
    # 有檔案 ≠ 檔案可用。實際踩到：yt-dlp 回 0、也產出了 .mp4，
    # 但只有 98KB、裡面一條 video 流都沒有（下載被截斷）。
    # 這種檔進到素材庫就是一格「無圖、尺寸空白」的垃圾，而且不會報錯。
    # 判斷依據只有一個：有沒有影像尺寸。壞檔的 duration 可能還是 6.0，
    # 用「duration 也沒有」當條件會漏掉它。
    # 只要聲音的下載本來就沒有影像軌，不能拿這條判斷（否則好好的 m4a 會被當壞檔刪掉）；
    # 這種檔改看有沒有音訊軌。
    if opts.audio_only:
        if not probe.get("has_audio"):
            final.unlink(missing_ok=True)
            _clean_partials(outdir, stem)
            raise MediaError("下載回來的檔案是壞的（沒有音訊軌），已刪除")
    elif not (probe.get("width") or probe.get("height")):
        final.unlink(missing_ok=True)
        _clean_partials(outdir, stem)
        raise MediaError("下載回來的檔案是壞的（沒有影像軌），已刪除")
    return {"path": str(final), "cached": False, **probe}


def _normalize_name(path: Path, stem: str) -> Path:
    """把 `stem.f30077.mp4` 這種檔名改成 `stem.mp4`。

    只在「改名後不會撞到既有檔案」時才動，避免蓋掉別的片段。
    """
    if not re.match(rf"^{re.escape(stem)}\.f\d+\.", path.name):
        return path
    target = path.with_name(f"{stem}{path.suffix}")
    if target.exists() and target.stat().st_size > 0:
        return path  # 已經有乾淨的，不要動
    try:
        path.rename(target)
        # info.json 也要跟著改，否則對不起來
        side = path.with_name(path.name.rsplit(".", 2)[0] + ".info.json")
        if side.exists():
            side.rename(target.with_name(target.stem + ".info.json"))
        return target
    except OSError:
        return path


def _clean_partials(outdir: Path, stem: str) -> int:
    """清掉中斷下載留下的暫存檔，回傳清掉幾個。"""
    removed = 0
    for leftover in outdir.glob(f"{stem}*.part"):
        try:
            leftover.unlink()
            removed += 1
        except OSError:
            pass
    # ffmpeg 合併時的中間檔
    for leftover in outdir.glob(f"{stem}*.temp.*"):
        try:
            leftover.unlink()
            removed += 1
        except OSError:
            pass
    return removed


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
