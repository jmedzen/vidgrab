"""一輪搜尋／一次切片：CLI 與 Web 共用同一套流程。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from . import media, report
from .version import full_version
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
                   drop_reposts: bool) -> list[Item]:
    fresh_after = ""
    if fresh_days:
        fresh_after = time.strftime("%Y%m%d", time.localtime(time.time() - fresh_days * 86400))
    out = []
    for item in items:
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
    emit: Emit = log,
) -> dict[str, Any]:
    queries = [q.strip() for q in queries if q and q.strip()]
    if not queries:
        raise SystemExit("要至少一個關鍵字。")

    sources = get_sources(platform)
    if not sources:
        raise SystemExit("沒有可用的來源。")
    exclude = [w for w in (exclude or []) if w]

    collected: list[Item] = []
    for keyword in queries:
        for src in sources:
            emit(f"· 搜尋「{keyword}」（{src.name}）…")
            try:
                found = src.search(
                    keyword,
                    limit=per_keyword,
                    order=order,
                    duration=duration,
                    exclude=exclude,
                    min_seconds=min_duration * 60 if min_duration else 0,
                    max_seconds=max_duration * 60 if max_duration else 0,
                )
            except SourceError as exc:
                emit(f"! {src.name} 搜「{keyword}」失敗：{exc}")
                continue
            emit(f"· {src.name} 「{keyword}」→ {len(found)} 支")
            collected.extend(found)

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
    if project.exists() and (project / "results.json").exists():
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
    grabbed = 0
    for n in indexes:
        item = items[n]
        emit(f"[{n + 1:02d}] {item.title} · {fmt_duration(item.duration)} · {item.license_label or '授權未標示'}")
        if item.license_kind in ("unknown", "none"):
            emit("     ⚠ 沒有授權資訊：公開使用前請自行確認")

        if frames:
            got = media.probe_frames(item, frames_dir, n=frame_count, opts=opts)
            if got:
                media.contact_sheet(got, frames_dir / f"{item.platform}_{item.vid}_sheet.jpg")
                item.thumbs = [str(f) for f in got]
                emit(f"     ▣ 抓了 {len(got)} 格畫面")
            else:
                emit("     ▣ 抓畫面失敗（可能需要登入 cookies）")

        if dry_run or frame_only:
            continue

        if not item.duration:
            try:
                info = media.probe_video(item.url, opts, timeout=180)
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

            def progress(info: dict[str, Any], _s=start) -> None:
                bits = []
                if info.get("percent") is not None:
                    bits.append(f"{info['percent']:.0f}%")
                if info.get("fragment"):
                    bits.append(f"分段 {info['fragment']}")
                if info.get("speed"):
                    bits.append(info["speed"])
                if info.get("eta"):
                    bits.append(f"剩 {info['eta']}")
                _emit_over(emit, "       " + " · ".join(bits) if bits else "       下載中…")

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
            item.clips.append(
                {
                    "path": info["path"],
                    "start": start,
                    "end": end,
                    "label": Path(info["path"]).name,
                    "size_bytes": info.get("size_bytes", 0),
                    "width": info.get("width"),
                    "height": info.get("height"),
                    "full": start is None,
                }
            )
            dims = f"{info.get('width')}×{info.get('height')}" if info.get("width") else ""
            what = "整支" if start is None else f"{fmt_ts(start)}–{fmt_ts(end or 0)}"
            emit(f"     ✓ {what} → {Path(info['path']).name} {dims} {human_size(info.get('size_bytes', 0))}")
            grabbed += 1
            save_project(project, manifest, items)

    save_project(project, manifest, items)
    emit(f"✔ 完成：{grabbed} 段 → {clips_dir}")
    return {"project": project, "grabbed": grabbed, "items": items}


def _emit_over(emit, text: str) -> None:
    """進度用「覆寫同一行」的方式送出，避免日誌被幾百行洗掉。"""
    try:
        emit("\r" + text)
    except Exception:  # noqa: BLE001
        pass


def _auto_point(item: Item, per: float) -> float:
    dur = item.duration or 0
    if dur <= 0:
        return 0.0
    start = max(3.0, dur * 0.12)
    return max(0.0, min(start, dur - per - 1)) if dur > per + 2 else 0.0
