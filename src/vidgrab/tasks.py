"""一輪搜尋／一次切片：CLI 與 Web 共用同一套流程。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from . import media, report
from .version import full_version
# 搜尋／抓畫面的重試次數。下載有自己的（media.MAX_DOWNLOAD_RETRIES = 10），
# 這裡是「抓清單」這種輕量請求，重試 3 次就夠，再多只是讓使用者乾等。
SEARCH_RETRIES = 3
FRAME_RETRIES = 2


def _retries(key: str, fallback: int) -> int:
    """重試次數以 config 為準；讀不到才用這裡的預設。"""
    from . import config

    try:
        return max(1, int(config.get(key, fallback)))
    except Exception:  # noqa: BLE001
        return fallback


def _timeout(key: str, fallback: float) -> float:
    from . import config

    try:
        return max(5.0, float(config.get(key, fallback)))
    except Exception:  # noqa: BLE001
        return fallback

from .models import (
    Item,
    fmt_duration,
    fmt_ts,
    human_size,
    iter_unique,
    log,
    now_iso,
    read_json,
    slugify,
    today,
    write_json,
)
from .sources import SourceError, get_sources

Emit = Callable[[str], None]


def _apply_filters(items: list[Item], *, fresh_days: int, min_duration: float,
                   max_duration: float, min_views: int, only_free: bool,
                   drop_reposts: bool, drop_tutorials: bool = True) -> list[Item]:
    fresh_after = ""
    if fresh_days:
        fresh_after = time.strftime("%Y%m%d", time.localtime(time.time() - fresh_days * 86400))
    from .models import looks_like_tutorial

    out = []
    for item in items:
        if drop_tutorials and looks_like_tutorial(item):
            continue
        if fresh_after and item.pubdate and item.pubdate < fresh_after:
            continue
        if min_duration and (item.duration or 0) < min_duration * 60:
            continue
        if max_duration and item.duration and item.duration > max_duration * 60:
            continue
        if min_views and (item.views or 0) < min_views:
            continue
        if only_free and item.license_kind not in ("free", "free-attribution"):
            continue
        if drop_reposts and item.repost:
            continue
        out.append(item)
    return out


def run_search(
    queries: list[str],
    *,
    platform: str = "both",
    per_keyword: int = 12,
    order: str = "smart",
    duration: str = "any",
    exclude: list[str] | None = None,
    fresh_days: int = 0,
    min_duration: float = 10,
    max_duration: float = 0,
    min_views: int = 0,
    only_free: bool = False,
    drop_reposts: bool = False,
    verify_license: bool = False,
    sort: str = "smart",
    name: str | None = None,
    out: str | None = None,
    replace: bool = False,
    emit: Emit = log,
) -> dict[str, Any]:
    queries = [q.strip() for q in queries if q and q.strip()]
    if not queries:
        raise SystemExit("要至少一個關鍵字。")

    sources = get_sources(platform)
    if not sources:
        raise SystemExit("沒有可用的來源。")
    exclude = [w for w in (exclude or []) if w]

    from .zh import search_forms, to_simplified

    collected: list[Item] = []
    for keyword in queries:
        for src in sources:
            # B 站是簡體站：繁體關鍵字常常一支都搜不到。
            # 原文與簡體都搜、取聯集，任一寫法命中都算數。
            forms = search_forms(keyword) if src.name == "bilibili" else [keyword]
            merged: list[Item] = []
            for form in forms:
                emit(f"· 搜尋「{form}」（{src.name}）…")
                try:
                    found = src.search(
                        form,
                        limit=per_keyword,
                        order=order,
                        duration=duration,
                        exclude=exclude,
                        min_seconds=min_duration * 60 if min_duration else 0,
                        max_seconds=max_duration * 60 if max_duration else 0,
                    )
                except SourceError as exc:
                    # 搜尋失敗多半是網路抽動或對方暫時限流 —— 這種重試有用。
                    # 一次失敗就跳過整個關鍵字，使用者會拿到一份莫名其妙少一半的清單。
                    last = exc
                    tries = _retries("search_retries", SEARCH_RETRIES)
                    for attempt in range(1, tries + 1):
                        wait = min(_timeout("retry_max_wait", 20.0),
                                   2.0 ** (attempt - 1))
                        emit(f"    ↻ {src.name} 搜「{form}」失敗，{wait:.0f} 秒後重試"
                             f"（{attempt}/{tries}）：{last}")
                        time.sleep(wait)
                        try:
                            found = src.search(
                                form,
                                limit=per_keyword,
                                order=order,
                                duration=duration,
                                exclude=exclude,
                                min_seconds=min_duration * 60 if min_duration else 0,
                                max_seconds=max_duration * 60 if max_duration else 0,
                            )
                            last = None
                            break
                        except SourceError as exc2:
                            last = exc2
                    if last is not None:
                        emit(f"! {src.name} 搜「{form}」重試 {tries} 次仍失敗：{last}")
                        continue
                    emit(f"· {src.name} 「{form}」→ {len(found)} 支（重試後成功）")
                emit(f"· {src.name} 「{form}」→ {len(found)} 支")
                merged.extend(found)

            if len(forms) > 1:
                total = len(iter_unique(merged))
                emit(f"· {src.name} 「{keyword}」合計 → {total} 支（含簡體 {to_simplified(keyword)}）")
            collected.extend(merged)

    collected = iter_unique(collected)
    if not collected:
        raise SystemExit("沒搜到東西。換個關鍵字、放寬長度或換平台再試。")

    items = _apply_filters(
        collected,
        fresh_days=fresh_days,
        min_duration=min_duration,
        max_duration=max_duration,
        min_views=min_views,
        only_free=only_free,
        drop_reposts=drop_reposts,
    )
    emit(f"· 篩選後 {len(items)} 支（原始 {len(collected)} 支）")

    if verify_license:
        for item in items:
            for src in sources:
                if src.name == item.platform:
                    src.detail(item)
                    break
            emit(f"· 確認 {item.vid} → {item.license_label or '未標示'}")

    items = report.rank(items, sort)

    project_name = name or f"{today()}-{slugify(queries[0], 20)}"
    root = Path(out).expanduser() if out else projects_root()
    project = root / project_name
    if project.exists() and (project / "results.json").exists() and not replace:
        # 預設不覆蓋：舊專案可能已經下載了片段，蓋掉會一起消失。
        # 但「同一個主題重搜」很常見，所以給 --replace 明確表達意圖。
        project = Path(f"{project}-{time.strftime('%H%M%S')}")
    project.mkdir(parents=True, exist_ok=True)

    manifest = {
        "queries": queries,
        "sources": [s.name for s in sources],
        "order": order,
        "per_keyword": per_keyword,
        "created": now_iso(),
        "tool": f"vidgrab {full_version()}",
        "filters": {
            "fresh_days": fresh_days,
            "min_duration": min_duration,
            "max_duration": max_duration,
            "min_views": min_views,
        },
    }
    save_project(project, manifest, items)
    emit(f"✔ 清單已建立：{project}")
    return {"project": project, "items": items, "manifest": manifest}


def projects_root() -> Path:
    import os

    return Path(os.environ.get("VIDGRAB_HOME") or Path.cwd() / "projects").expanduser()


def scrub_metadata(project: Path) -> int:
    """把既有結果裡的技術標記（yt:cc=on 之類）清掉，並補上可用的關鍵字。

    這是給「metadata 功能上線前就存在的專案」用的回填；不改其他欄位。
    """
    from .models import _is_tech_tag

    manifest, items = load_project(project)
    touched = 0
    for item in items:
        before = list(item.tags)
        item.tags = [t for t in item.tags if not _is_tech_tag(t)]
        if item.tags != before:
            touched += 1
    if touched:
        save_project(project, manifest, items)
    return touched


def load_project(project: Path) -> tuple[dict[str, Any], list[Item]]:
    manifest = read_json(project / "manifest.json", {}) or {}
    raw = read_json(project / "results.json", []) or []
    return manifest, [Item(**r) for r in raw]


def save_project(project: Path, manifest: dict[str, Any], items: list[Item]) -> None:
    manifest["updated"] = now_iso()
    write_json(project / "manifest.json", manifest)
    write_json(project / "results.json", [i.to_dict() for i in items])
    report.write_html(project / "index.html", items, project, manifest)
    report.write_csv(project / "clips.csv", items, project)
    report.write_readme(project / "README.md", items, manifest)


def run_pick(
    project: Path,
    indexes: list[int],
    *,
    per: float = 15.0,
    starts: list[float] | None = None,
    ranges: list[tuple[float, float]] | None = None,
    full: bool = False,
    frames: bool = False,
    frame_count: int = 3,
    height: int = 1080,
    audio: bool = False,
    audio_format: str = "m4a",
    cookies: str | None = None,
    cookies_from_browser: str | None = None,
    fmt: str | None = None,
    dry_run: bool = False,
    frame_only: bool = False,
    emit: Emit = log,
) -> dict[str, Any]:
    manifest, items = load_project(project)
    if not items:
        raise SystemExit("這個專案裡沒有候選影片。")

    if not cookies and not cookies_from_browser:
        try:
            from . import auth

            if auth.logged_in():
                cookies = str(auth.cookie_file())
        except Exception:  # noqa: BLE001
            pass

    opts = media.GrabOptions(
        height=height,
        audio_only=audio,
        audio_format=audio_format,
        cookies=cookies,
        cookies_from_browser=cookies_from_browser,
        extra_format=fmt,
    )
    sources = {}
    for src in get_sources("both"):
        sources[src.name] = src

    clips_dir = project / "clips"
    frames_dir = project / "frames"
    thumb_dir = project / "thumbs"
    grabbed = 0
    order = list(indexes)  # 預先算好位置，進度回呼會很頻繁
    for n in order:
        pos = order.index(n) + 1
        if not 0 <= n < len(items):
            # 呼叫端已經擋過，這裡是最後一道：寧可回一句人話，
            # 也不要讓 IndexError 冒到工作層變成「完成了但什麼都沒有」。
            emit(f"✗ 編號 {n} 超出範圍（0–{len(items) - 1}），略過")
            continue
        item = items[n]
        emit(f"[{n + 1:02d}] {item.title} · {fmt_duration(item.duration)} · {item.license_label or '授權未標示'}")
        if item.license_kind in ("unknown", "none"):
            emit("     ⚠ 沒有授權資訊：公開使用前請自行確認")

        if frames:
            # 抽畫面只是輔助：抓不到就算了，不能讓它把整個作業帶走。
            # 真的發生過：YouTube 長片抽幀逾時 → 整批 batch 回 internal，
            # 明明片段已經下載成功，使用者卻收到失敗。
            got: list = []
            frame_tries = _retries("frame_retries", FRAME_RETRIES)
            for attempt in range(1, frame_tries + 1):
                try:
                    got = media.probe_frames(item, frames_dir, n=frame_count, opts=opts)
                    break
                except Exception as exc:  # noqa: BLE001
                    got = []
                    if attempt < frame_tries:
                        emit(f"     ↻ 抓畫面失敗（{type(exc).__name__}），重試"
                             f"（{attempt}/{frame_tries}）")
                        time.sleep(1.0 * attempt)
                    else:
                        emit(f"     ▣ 抓畫面失敗（{type(exc).__name__}）："
                             "略過，不影響已下載的片段")
            if got:
                try:
                    media.contact_sheet(got, frames_dir / f"{item.platform}_{item.vid}_sheet.jpg")
                except Exception:  # noqa: BLE001
                    pass
                item.thumbs = [str(f) for f in got]
                emit(f"     ▣ 抓了 {len(got)} 格畫面")
            elif frames:
                emit("     ▣ 抓畫面失敗（可能需要登入 cookies），片段照樣保留")

        if dry_run or frame_only:
            continue

        if not item.duration:
            try:
                info = media.probe_video(item.url, opts,
                                               timeout=_timeout("probe_timeout", 180.0))
                item.duration = info.get("duration") or item.duration
            except Exception as exc:  # noqa: BLE001
                item.error = f"取不到影片資訊：{exc}"
        # 要下載哪幾段：(start, end)；None = 整支
        jobs: list[tuple[float | None, float | None]] = []
        if full:
            jobs.append((None, None))
        if ranges:
            jobs.extend(ranges)
        if not full and not ranges:
            for start in (starts or [_auto_point(item, per)]):
                jobs.append((start, start + per))

        for start, end in jobs:
            if start is not None and item.duration and end and end > item.duration + 1:
                emit(f"     ↷ 跳過 {fmt_ts(start)}：超過影片長度")
                continue

            # 追蹤這一段目前為止看過的最高百分比。
            # 切片下載的心跳與 yt-dlp 的真實回報會交錯出現，
            # 真實那筆常常沒有 percent，直接送出去會讓進度條倒退。
            best = {"pct": 0.0, "estimated": False}

            def progress(info: dict[str, Any], _s=start, _n=n, _end=end, _pos=pos) -> None:
                pct = info.get("percent")
                if pct is not None:
                    if pct >= best["pct"]:
                        best["pct"] = pct
                        best["estimated"] = bool(info.get("estimated"))
                    else:
                        pct = best["pct"]  # 不讓它倒退
                else:
                    # 沒百分比（真實回報只有位元組數）就沿用目前進度
                    pct = best["pct"] if best["pct"] > 0 else None

                bits = []
                if info.get("percent") is not None:
                    bits.append(f"{info['percent']:.0f}%")
                if info.get("fragment"):
                    bits.append(f"分段 {info['fragment']}")
                if info.get("speed"):
                    bits.append(info["speed"])
                if info.get("eta"):
                    bits.append(f"剩 {info['eta']}")
                # 進度行帶結構化資料：前端不必再用正則從中文裡撈百分比。
                # 以 \r 開頭代表「覆寫同一行」，沿用既有的 console 行為。
                payload = {
                    "kind": "download",
                    "index": _n,
                    "position": _pos,
                    "total": len(indexes),
                    "title": item.title,
                    "asset_id": item.asset_id(),
                    "percent": pct,
                    "estimated": bool(info.get("estimated", False)),
                    "speed": info.get("speed") or "",
                    "eta": info.get("eta") or "",
                    "fragment": info.get("fragment") or "",
                    "start": _s,
                    "end": _end,
                }
                text = "       " + " · ".join(bits) if bits else "       下載中…"
                _emit_over(emit, text, payload)

            try:
                if start is None:
                    info = media.grab_full(
                        item, clips_dir, opts, label=slugify(item.author or "", 12),
                        on_progress=progress,
                    )
                else:
                    info = media.grab_clip(
                        item, start, end, clips_dir, opts,
                        label=slugify(item.author or "", 12), on_progress=progress,
                    )
            except media.MediaError as exc:
                item.error = str(exc)
                emit(f"     ✗ {exc}")
                continue

            item.clips = [
                c for c in item.clips
                if not (c.get("start") == start and c.get("end") == end)
            ]
            from .models import clip_id

            # 每個片段下載完就立刻抽一格縮圖。
            # 縮圖一定從這個片段檔本身抽，所以畫面必然對得上
            # （以前是去整支影片的均勻位置抓，跟片段毫無關係）。
            cid = clip_id(item.platform, item.vid, start, end)
            thumb_path = thumb_dir / f"{_safe_name(cid)}.jpg"
            thumb = None
            try:
                got_thumb = media.thumb_from_clip(Path(info["path"]), thumb_path)
                thumb = str(got_thumb) if got_thumb else None
            except Exception:  # noqa: BLE001
                thumb = None
            if not thumb:
                emit("     ▣ 縮圖沒抽出來（片段仍然可用）")

            item.clips.append(
                {
                    "id": cid,
                    "asset_id": item.asset_id(),
                    "thumb": thumb,
                    "path": info["path"],
                    "start": start,
                    "end": end,
                    "label": Path(info["path"]).name,
                    "size_bytes": info.get("size_bytes", 0),
                    "width": info.get("width"),
                    "height": info.get("height"),
                    "full": start is None,
                    "cached": bool(info.get("cached")),
                    # 直式／橫式：剪進橫式影片之前先看一眼，免得整段都是黑邊
                    "orientation": (
                        "portrait" if (info.get("height") or 0) > (info.get("width") or 0)
                        else "landscape" if info.get("width") else ""
                    ),
                }
            )
            dims = f"{info.get('width')}×{info.get('height')}" if info.get("width") else ""
            if (info.get("height") or 0) > (info.get("width") or 0):
                emit(f"     ▤ 直式畫面（{dims}）：如果要剪橫式影片，記得裁切或用模糊底")
            what = "整支" if start is None else f"{fmt_ts(start)}–{fmt_ts(end or 0)}"
            grabbed += 1
            # 先存檔再回報：emit 可能拋錯（例如批次逾時），
            # 但抓到的片段一定要先落地，否則檔案在、清單卻沒有。
            save_project(project, manifest, items)
            # 帶一份結構化事件：前端收到就立刻把這支片段掛上去。
            # 只送文字的話，使用者要等整批跑完才看得到已經抓好的片段
            # （真的發生過：batch 10 支，前 9 支都好了，畫面卻整片空白）。
            _emit_over(
                emit,
                f"     ✓ {what} → {Path(info['path']).name} {dims} {human_size(info.get('size_bytes', 0))}",
                {
                    "kind": "clip",
                    "index": n,
                    "title": item.title,
                    "name": Path(info["path"]).name,
                    "grabbed": grabbed,
                    "total": len(indexes),
                },
            )

    save_project(project, manifest, items)
    emit(f"✔ 完成：{grabbed} 段 → {clips_dir}")
    return {"project": project, "grabbed": grabbed, "items": items}


def _safe_name(text: str) -> str:
    """把 clip id 變成安全的檔名。

    `bilibili:BV1xx#30-45` 的 `:` 和 `#` 在檔名裡會出問題，換成 `_`。
    """
    out = []
    for ch in str(text):
        out.append(ch if (ch.isalnum() or ch in "-_.") else "_")
    return "".join(out) or "clip"


def _emit_over(emit, text: str, payload: dict | None = None) -> None:
    """進度用「覆寫同一行」的方式送出，避免日誌被幾百行洗掉。

    可以附一份結構化 payload：前端靠它畫進度條，不必解析中文。
    """
    line = "\r" + text
    if payload is not None:
        line += "\x00" + json.dumps(payload, ensure_ascii=False)
    try:
        emit(line)
    except Exception:  # noqa: BLE001
        # 進度行送不出去不該讓下載中斷，但也不該假裝沒事。
        # json 沒 import 這種錯就是在這裡被吞掉的（真的發生過）。
        pass


def _auto_point(item: Item, per: float) -> float:
    dur = item.duration or 0
    if dur <= 0:
        return 0.0
    start = max(3.0, dur * 0.12)
    return max(0.0, min(start, dur - per - 1)) if dur > per + 2 else 0.0


def backfill_thumbs(project: Path, *, emit=print) -> dict:
    """幫已經下載好的片段補縮圖。

    縮圖是後來才加的功能，舊專案的片段沒有。
    這裡從片段檔本身抽，所以畫面一定對得上，不會出現「圖文不符」。
    """
    from .media import thumb_from_clip

    manifest, items = load_project(project)
    thumb_dir = project / "thumbs"
    made = kept = failed = 0

    for item in items:
        for clip in item.clips:
            cid = clip.get("id") or ""
            path = Path(clip.get("path") or "")
            if not cid or not path.exists():
                failed += 1
                continue
            dest = thumb_dir / f"{_safe_name(cid)}.jpg"
            existing = clip.get("thumb")
            if existing and Path(existing).exists():
                kept += 1
                continue
            try:
                got = thumb_from_clip(path, dest)
            except Exception:  # noqa: BLE001
                got = None
            if got:
                clip["thumb"] = str(got)
                made += 1
            else:
                failed += 1

    if made:
        save_project(project, manifest, items)
    emit(f"  縮圖：新增 {made}、已存在 {kept}、失敗 {failed}")
    return {"made": made, "kept": kept, "failed": failed}
