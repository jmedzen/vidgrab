"""給 AI agent 的素材庫介面。

設計原則（跟人用的 HTML／CSV 刻意不同）：

1. **一次呼叫拿到全部** — agent 不擅長多輪探索，`vault` 一個指令就回完整狀態
2. **穩定 id** — 每個資產與片段都能被長期引用，重掃描也不變
3. **絕對路徑** — agent 拿到就能直接用，不用自己拼路徑
4. **結構化錯誤** — 回 `{code, message, retryable}`，不要 traceback
5. **冪等** — 同樣的請求重跑不會重複下載，回 `cached: true`
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .models import Item, human_size, read_json, search_terms, contact_block


def _rel_or_abs(path: str, root: Path) -> str:
    """agent 需要絕對路徑；這裡一律回絕對路徑。"""
    p = Path(path)
    return str(p if p.is_absolute() else (root / p).resolve())


def file_url(path_like: str | None) -> str:
    """素材庫裡的檔案 → `/file/<專案>/<相對路徑>`（HTTP 取檔用）；不在素材庫內或不存在就回空字串。

    agent 不一定跟 vidgrab 同一台機器／同一個檔案系統，所以除了絕對路徑也要給 URL。
    """
    import urllib.parse

    from . import tasks

    if not path_like:
        return ""
    try:
        p = Path(path_like).resolve()
        rel = p.relative_to(tasks.projects_root().resolve())
    except (OSError, ValueError):
        return ""
    if len(rel.parts) < 2 or not p.is_file():
        return ""
    return (f"/file/{urllib.parse.quote(rel.parts[0])}/"
            f"{urllib.parse.quote('/'.join(rel.parts[1:]))}")


def clip_summary(clip: dict[str, Any], root: Path, item: Item | None = None) -> dict[str, Any]:
    """一個片段對 agent 有用的全部資訊。

    id 若不存在（舊資料）就當場算出來——不必先跑遷移，agent 也拿得到穩定鍵。
    """
    from .models import clip_id

    start, end = clip.get("start"), clip.get("end")
    path = _rel_or_abs(clip.get("path") or "", root)
    cid = clip.get("id") or ""
    aid = clip.get("asset_id") or ""
    if item is not None:
        aid = aid or item.asset_id()
        cid = cid or clip_id(item.platform, item.vid, start, end)
    return {
        "id": cid,
        "asset_id": aid,
        "path": path,
        "exists": Path(path).exists() if path else False,
        "start": start,
        "end": end,
        "is_full": start is None,
        "duration": (round(float(end) - float(start), 3)
                     if start is not None and end is not None else None),
        "label": clip.get("label") or "",
        "size_bytes": clip.get("size_bytes") or 0,
        "size_human": human_size(clip.get("size_bytes") or 0),
        "width": clip.get("width"),
        "height": clip.get("height"),
        # 直式片段剪進橫式影片會整段黑邊，agent 選素材時要先知道
        "orientation": clip.get("orientation") or (
            "portrait" if (clip.get("height") or 0) > (clip.get("width") or 0)
            else "landscape" if clip.get("width") else ""
        ),
        "cached": bool(clip.get("cached")),
        # 縮圖路徑：任務清單要看到畫面，就得把這個帶出去
        "thumb": _rel_or_abs(clip.get("thumb") or "", root),
        "has_thumb": bool(clip.get("thumb")) and Path(clip["thumb"]).exists(),
        # HTTP 取檔網址（跨機器／容器時用這個，不要用 path）
        "url": file_url(path),
        "thumb_url": file_url(_rel_or_abs(clip.get("thumb") or "", root)) if clip.get("thumb") else "",
    }


def item_summary(item: Item, root: Path, *, with_description: bool = False) -> dict[str, Any]:
    """一支素材對 agent 有用的全部資訊。"""
    out: dict[str, Any] = {
        "id": item.asset_id(),
        "platform": item.platform,
        "vid": item.vid,
        "title": item.title,
        "author": item.author,
        "author_url": item.author_url,
        "webpage_url": item.webpage_url or item.url,
        "duration": item.duration,
        "width": item.width,
        "height": item.height,
        "pubdate": item.pubdate,
        "license": {
            "kind": item.license_kind,
            "label": item.license_label,
            "name": item.license_name,
        },
        "tags": list(item.tags),
        "search_terms": search_terms(item),
        "credit_hint": contact_block(item)["credit_hint"],
        "clips": [clip_summary(c, root, item) for c in item.clips],
    }
    if with_description:
        out["description"] = item.description_full or item.description
    return out


def project_summary(
    project: Path, *, with_items: bool = False, with_description: bool = False
) -> dict[str, Any]:
    """一個專案對 agent 有用的全部資訊。"""
    from . import tasks

    manifest, items = tasks.load_project(project)
    root = tasks.projects_root()

    disk_bytes = 0
    for it in items:
        for c in it.clips:
            try:
                p = Path(c.get("path") or "")
                if p.exists():
                    disk_bytes += p.stat().st_size
            except OSError:
                pass

    out: dict[str, Any] = {
        "name": project.name,
        "path": str(project.resolve()),
        "queries": manifest.get("queries") or [],
        "created": manifest.get("created") or "",
        "counts": {
            "candidates": len(items),
            "with_clips": sum(1 for i in items if i.clips),
            "clips": sum(len(i.clips) for i in items),
        },
        "bytes_on_disk": disk_bytes,
        "size_human": human_size(disk_bytes),
    }
    if with_items:
        out["items"] = [item_summary(i, root, with_description=with_description) for i in items]
    return out


def vault_manifest(*, with_items: bool = False, with_description: bool = False) -> dict[str, Any]:
    """整個素材庫的快照——agent 的主要入口。

    一次呼叫就拿到：有哪些專案、每個專案有什麼、哪些片段已經在磁碟上。
    """
    from . import library, tasks

    root = tasks.projects_root()
    folders = sorted(p.name for p in root.glob("*") if (p / "manifest.json").exists())

    lib = library.load()
    lib, changed = library.sync(lib, folders)
    if changed:
        library.save(lib)

    projects = []
    for folder in lib.get("order", []):
        p = root / folder
        meta = library.entry(lib, folder)
        if not (p / "manifest.json").exists():
            projects.append({
                "name": folder,
                "title": meta["title"],
                "pinned": meta["pinned"],
                "missing": True,
                "path": str(p.resolve()),
                # 資料夾不見了：形狀仍要跟正常專案一致，
                # 否則呼叫方取值時會 KeyError（真的發生過）。
                "counts": {"candidates": 0, "clips": 0},
                "bytes_on_disk": 0,
                "size_human": "0B",
                "queries": meta.get("queries") or [],
                "created": meta.get("created") or "",
                "items": [] if with_items else [],
            })
            continue
        summary = project_summary(p, with_items=with_items, with_description=with_description)
        summary["title"] = meta["title"]
        summary["pinned"] = meta["pinned"]
        summary["missing"] = False
        projects.append(summary)

    total_clips = sum(p.get("counts", {}).get("clips", 0) for p in projects)
    total_bytes = sum(p.get("bytes_on_disk", 0) for p in projects)

    return {
        "schema": "vidgrab.vault/1",
        "root": str(root.resolve()),
        "totals": {
            "projects": len(projects),
            "clips": total_clips,
            "bytes": total_bytes,
            "size_human": human_size(total_bytes),
        },
        "projects": projects,
    }


def find_clip(vault: dict[str, Any], clip_id: str) -> dict[str, Any] | None:
    """用片段 id 找回那個片段（含它屬於哪支素材）。"""
    for proj in vault.get("projects", []):
        for item in proj.get("items", []) or []:
            for clip in item.get("clips", []):
                if clip.get("id") == clip_id:
                    return {"project": proj["name"], "item": item, "clip": clip}
    return None


def search_assets(
    vault: dict[str, Any], query: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    """**在已經下載的素材裡**搜尋。

    這跟 `search` 指令不同：`search` 是去平台找新片，
    這個是在自己的 vault 裡找「我手上有什麼可以用」。

    比對：標題、作者、標籤、搜尋詞。分數高的排前面。
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    terms = [t for t in q.replace("，", " ").replace(",", " ").split() if t]

    hits: list[dict[str, Any]] = []
    for proj in vault.get("projects", []):
        for item in proj.get("items", []) or []:
            hay_parts = [
                (item.get("title") or "").lower(),
                (item.get("author") or "").lower(),
                " ".join(item.get("tags") or []).lower(),
                " ".join(item.get("search_terms") or []).lower(),
            ]
            hay = " ".join(hay_parts)
            matched = [t for t in terms if t in hay]
            if not matched:
                continue
            score = len(matched) / len(terms)
            if q in (item.get("title") or "").lower():
                score += 0.5
            hits.append({
                "project": proj["name"],
                "score": round(score, 3),
                "matched": matched,
                "item": item,
            })

    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:limit]


def fingerprint(paths: list[str]) -> str:
    """一組檔案的指紋，給「這次抓到的東西跟上次一樣嗎」用。"""
    h = hashlib.sha256()
    for p in sorted(paths):
        try:
            st = Path(p).stat()
            h.update(f"{p}:{st.st_size}".encode())
        except OSError:
            h.update(f"{p}:missing".encode())
    return h.hexdigest()[:16]


def find_by_id(needle: str) -> dict[str, Any] | None:
    """用 asset id（`bilibili:BV1xx`）或片段 id（`bilibili:BV1xx#60-75`）找回素材。"""
    vault = vault_manifest(with_items=True)
    for proj in vault["projects"]:
        for item in proj.get("items", []) or []:
            if item["id"] == needle:
                return {"project": proj["name"], "item": item}
            for clip in item.get("clips", []):
                if clip["id"] == needle:
                    return {"project": proj["name"], "item": item, "clip": clip}
    return None


def vault_query(*, project: str = "", query: str = "", limit: int = 20,
                with_items: bool = True, with_description: bool = False) -> dict[str, Any]:
    """`vault` 的共用實作（CLI 與 HTTP 同一份）。找不到專案丟 NotFound。"""
    from .models import NotFound

    manifest = vault_manifest(with_items=with_items or bool(query), with_description=with_description)
    if project:
        for p in manifest["projects"]:
            if p["name"] == project:
                return p
        raise NotFound(f"找不到專案：{project}")
    if query:
        manifest["query"] = query
        manifest["matches"] = [
            {"project": h["project"], "score": h["score"], "matched": h["matched"], "item": h["item"]}
            for h in search_assets(manifest, query, limit=limit)
        ]
        del manifest["projects"]  # 只回命中，agent 才不用自己篩
    return manifest


def _clip_key(c: dict[str, Any]) -> tuple:
    # 用「範圍」當去重鍵，不用 label／id：舊資料沒有 id，混用會讓同一個片段被當成新的
    return (c.get("start"), c.get("end"))


def clip_keys(items: list[Item], targets: list[int]) -> dict[int, set]:
    """記下這些素材目前有哪些片段，之後用 new_clips() 算出「這次新增的」。"""
    return {i: {_clip_key(c) for c in items[i].clips} for i in targets}


def new_clips(project: Path, targets: list[int], before: dict[int, set]) -> list[dict[str, Any]]:
    """每個目標素材：這次新增的片段（含 id、url、duration、orientation、授權）與錯誤。"""
    from . import tasks

    _, after = tasks.load_project(project)
    out = []
    for i in targets:
        item = after[i]
        fresh = [c for c in item.clips if _clip_key(c) not in before.get(i, set())]
        credit = contact_block(item)["credit_hint"]
        clips = []
        for c in fresh:
            summary = clip_summary(c, project, item)
            summary["license"] = {"kind": item.license_kind, "label": item.license_label}
            summary["credit_hint"] = credit
            clips.append(summary)
        out.append({
            "index": i,
            "asset_id": item.asset_id(),
            "title": item.title,
            "new_clips": clips,
            "total_clips": len(item.clips),
            "error": item.error or None,
        })
    return out


def resolve_targets(items: list[Item], *, indexes=None, asset_ids=None,
                    top: int | None = None, all_items: bool = False) -> list[int]:
    """批次要抓哪幾支：索引、asset id、前 N 支、或全部。不合法就丟 BadInput。"""
    from .models import BadInput

    if asset_ids:
        lookup = {it.asset_id(): n for n, it in enumerate(items)}
        missing = [a for a in asset_ids if a not in lookup]
        if missing:
            raise BadInput(f"這個專案沒有這些素材：{missing}", hint="用 vault 查 asset id")
        return [lookup[a] for a in asset_ids]
    if indexes:
        try:
            idx = [int(i) for i in indexes]
        except (TypeError, ValueError):
            raise BadInput("indexes 必須是整數陣列") from None
        bad = [i for i in idx if i < 0 or i >= len(items)]
        if bad:
            raise BadInput(f"編號超出範圍（0–{len(items) - 1}）：{bad}")
        return idx
    if top:
        return list(range(min(int(top), len(items))))
    if all_items:
        return list(range(len(items)))
    return []


def run_batch(project: Path, targets: list[int], *, timeout: float | None = None,
              emit=None, **pick_opts) -> dict[str, Any]:
    """批次抓片（CLI `batch` 與 HTTP `/api/batch` 共用），回 `vidgrab.batch/1`。

    - 冪等：同一個範圍已經抓過就不會重下載，new_clips 為空
    - 部分成功：每支各自回報
    - timeout：到期就中止正在跑的下載（殺掉 yt-dlp），已抓好的保留，
      回傳 `timed_out: true` 與結構化的 `error`，而不是整個丟例外
    """
    import time

    from . import tasks
    from .models import VidgrabError

    _, items = tasks.load_project(project)
    before = clip_keys(items, targets)
    started = time.time()
    deadline = started + float(timeout) if timeout else None
    sink = emit or (lambda _msg: None)

    def tick(msg: str) -> None:
        if deadline and time.time() > deadline:
            raise VidgrabError(
                f"批次超過 {timeout} 秒，已中止", code="timeout", retryable=True,
                hint="已經抓好的片段都會保留；調高 timeout 或縮小批次重試",
            )
        sink(msg)

    error = None
    summary: Any = None
    try:
        summary = tasks.run_pick(project, targets, emit=tick, **pick_opts)
    except VidgrabError as exc:
        if exc.code != "timeout":
            raise
        error = exc.to_dict()

    results = new_clips(project, targets, before)
    failed = sum(1 for r in results if r["error"])
    out = {
        "schema": "vidgrab.batch/1",
        "project": project.name,
        "requested": len(targets),
        "ok": len(results) - failed,
        "failed": failed,
        "elapsed_seconds": round(time.time() - started, 2),
        "dry_run": bool(pick_opts.get("dry_run")),
        "timed_out": error is not None,
        "results": results,
    }
    if summary is not None:
        out["summary"] = summary
    if error:
        out["error"] = error
    return out


FRAME_FORMATS = ("jpg", "png")


def _locate(needle: str, project: str = "") -> tuple[Path, list[Item], int, dict[str, Any] | None]:
    """id（asset id 或片段 id）→ (專案資料夾, 該專案的候選, 素材索引, 片段或 None)。"""
    from . import tasks
    from .models import NotFound

    asset_id, _, _span = needle.partition("#")
    root = tasks.projects_root()
    names = [project] if project else sorted(
        p.name for p in root.glob("*") if (p / "manifest.json").exists())
    for name in names:
        folder = root / name
        if not (folder / "manifest.json").exists():
            continue
        _, items = tasks.load_project(folder)
        for idx, item in enumerate(items):
            if item.asset_id() != asset_id:
                continue
            if "#" not in needle:
                return folder, items, idx, None
            from .models import clip_id

            for c in item.clips:
                cid = c.get("id") or clip_id(item.platform, item.vid, c.get("start"), c.get("end"))
                if cid == needle:
                    return folder, items, idx, c
    raise NotFound(f"找不到：{needle}", hint="id 長得像 bilibili:BV1xx 或 bilibili:BV1xx#60-75；也可以指定 project")


def grab_frame(needle: str, at: float | None = None, *, width: int = 640, fmt: str = "jpg",
               project: str = "") -> dict[str, Any]:
    """單格畫面：回 PNG/JPG 的絕對路徑與 `/file/...` 網址（封面、縮圖、分鏡用）。

    - `needle` 是片段 id：從那個已下載的片段抽（本機 ffmpeg，很快）。`at` 是**原影片的秒數**
      （跟片段 id 裡的 60-75 同一條時間軸），不給就取片段正中間。
    - `needle` 是 asset id：若已下載的片段涵蓋 `at` 就從片段抽，否則只下載該時間點的 1 秒去抽
      （需要網路，成本很低）。這種一定要給 `at`。
    - 同一個 (素材, 秒數, 寬度, 格式) 只會做一次，之後直接回快取。
    """
    from . import media, tasks
    from .models import BadInput, DownloadFailed, clip_id

    fmt = (fmt or "jpg").lower().lstrip(".")
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in FRAME_FORMATS:
        raise BadInput(f"format 只能是 {'／'.join(FRAME_FORMATS)}", hint="例如 jpg")
    try:
        width = int(width)
    except (TypeError, ValueError):
        raise BadInput("width 必須是整數") from None
    if not 16 <= width <= 3840:
        raise BadInput("width 要在 16–3840 之間")
    if at is not None:
        try:
            at = float(at)
        except (TypeError, ValueError):
            raise BadInput("at 必須是秒數") from None
        if at < 0:
            raise BadInput("at 不能是負的")

    folder, items, idx, clip = _locate(str(needle), project)
    item = items[idx]
    root = tasks.projects_root()

    def covering(c: dict[str, Any], t: float) -> bool:
        p = Path(_rel_or_abs(c.get("path") or "", root))
        if not p.exists():
            return False
        if c.get("start") is None:
            return True  # 整支
        return float(c["start"]) - 0.01 <= t <= float(c.get("end") or 0) + 0.01

    if clip is not None:
        if at is None:
            if clip.get("start") is None:
                dur = float(media.ffprobe(Path(_rel_or_abs(clip.get("path") or "", root))).get("duration") or 0)
                at = dur / 2
            else:
                at = (float(clip["start"]) + float(clip.get("end") or clip["start"])) / 2
        if not covering(clip, at):
            raise BadInput(
                f"at={at} 不在這個片段內（{clip.get('start')}–{clip.get('end')} 秒）或檔案不見了",
                hint="at 是原影片的秒數；要別的時間點請改用 asset id（會另外下載那一秒）")
        source_clip = clip
    else:
        if at is None:
            raise BadInput("用 asset id 抽畫面一定要給 at（秒）", hint="例如 at: 12.5")
        if item.duration and at > float(item.duration):
            raise BadInput(f"at={at} 超過影片長度 {item.duration} 秒")
        source_clip = next((c for c in item.clips if covering(c, at)), None)

    frames_dir = folder / "frames"
    name = f"{item.platform}_{item.vid}_at{int(round(at * 1000)):08d}_w{width}.{fmt}"
    dest = frames_dir / name
    source = "cache"
    if not (dest.exists() and dest.stat().st_size > 0):
        if source_clip is not None:
            video = Path(_rel_or_abs(source_clip.get("path") or "", root))
            offset = 0.0 if source_clip.get("start") is None else at - float(source_clip["start"])
            if source_clip.get("start") is None:
                offset = at
            if media.extract_frame(video, dest, offset, width) is None:
                raise DownloadFailed("抽畫面失敗（ffmpeg）", hint="檔案可能壞了，試試重新下載該片段")
            source = "clip"
        else:
            import shutil
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                got = media.probe_frames(item, Path(tmp), points=[at], width=width)
                if not got:
                    raise DownloadFailed("抓不到那個時間點的畫面", hint="可能需要登入 B 站，或影片暫時不可用")
                frames_dir.mkdir(parents=True, exist_ok=True)
                if fmt == "jpg":
                    shutil.move(str(got[0]), dest)
                else:  # probe_frames 只產 jpg；要 png 就轉一次
                    proc = media._run(["ffmpeg", "-v", "error", "-y", "-i", str(got[0]), str(dest)], timeout=60)
                    if proc.returncode != 0:
                        raise DownloadFailed("轉檔失敗（ffmpeg）")
            source = "remote"

    probe = media.ffprobe(dest)
    return {
        "schema": "vidgrab.frame/1",
        "id": f"{item.asset_id()}@{at:g}",
        "asset_id": item.asset_id(),
        "clip_id": (source_clip.get("id") or clip_id(item.platform, item.vid, source_clip.get("start"),
                                                      source_clip.get("end"))) if source_clip else None,
        "project": folder.name,
        "at": at,
        "width": probe.get("width") or width,
        "height": probe.get("height"),
        "format": fmt,
        "source": source,
        "path": str(dest.resolve()),
        "url": file_url(str(dest)),
        "license": {"kind": item.license_kind, "label": item.license_label},
        "credit_hint": contact_block(item)["credit_hint"],
    }
