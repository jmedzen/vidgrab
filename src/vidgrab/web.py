"""Web UI：本機網頁介面，用 SSE 即時回報搜尋／切片進度。"""

from __future__ import annotations

import html
import json
import queue
import select
import socket
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__, media, report, tasks
from .models import Item, fmt_duration, human_size, log, slugify, today

PLATFORM_LABEL = {"bilibili": "B 站", "youtube": "YouTube"}
LICENSE_SHORT = {
    "free": "可自由使用",
    "free-attribution": "可用需標註",
    "check": "需確認授權",
    "unknown": "未知",
    "none": "無資訊",
}

JOBS: dict[str, dict[str, Any]] = {}
QR: dict[str, float] = {}
LOCK = threading.Lock()


# ---------------------------------------------------------------- jobs


class Job:
    def __init__(self, kind: str, label: str):
        self.id = f"{int(time.time() * 1000)}-{abs(hash(label)) % 10000}"
        self.kind = kind
        self.label = label
        self.messages: list[str] = []
        self.done = False
        self.error = ""
        self.result: dict[str, Any] = {}
        self.queue: queue.Queue = queue.Queue()
        self.started = time.time()
        self.finished = 0.0

    def emit(self, message: str) -> None:
        # 日誌限長：長時間下載會產生上萬行，全留在記憶體裡沒有意義
        if len(self.messages) < MAX_JOB_MESSAGES:
            self.messages.append(message)
        self.queue.put(("log", message))

    def finish(self, result: dict[str, Any]) -> None:
        self.done = True
        self.finished = time.time()
        self.result = result
        self.queue.put(("done", result))

    def fail(self, message: str) -> None:
        self.done = True
        self.finished = time.time()
        self.error = message
        self.queue.put(("error", message))

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "done": self.done,
            "error": self.error,
            "messages": self.messages[-200:],
            "result": _jsonable(self.result),
            "elapsed": round(time.time() - self.started, 1),
        }


def _jsonable(data: Any) -> Any:
    if isinstance(data, Path):
        return str(data)
    if isinstance(data, Item):
        return data.to_dict()
    if isinstance(data, dict):
        return {k: _jsonable(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_jsonable(v) for v in data]
    return data


MAX_FINISHED_JOBS = 40
JOB_TTL = 30 * 60          # 完成後保留 30 分鐘，足夠你回頭看結果
MAX_JOB_MESSAGES = 2000    # 單一 job 的日誌上限，避免長時間下載把記憶體吃光


def _prune_jobs() -> None:
    """清掉已經結束的舊 job。

    這裡原本只寫不刪：每跑一次搜尋／下載就留一個 Job（含完整結果與日誌），
    開著一整天會一直長大。
    """
    now = time.time()
    with LOCK:
        dead = [
            jid for jid, job in JOBS.items()
            if getattr(job, "done", False)
            and now - getattr(job, "finished", getattr(job, "started", now)) > JOB_TTL
        ]
        for jid in dead:
            JOBS.pop(jid, None)

        # 就算都還很新，也不要無限累積
        finished = sorted(
            ((getattr(j, "finished", getattr(j, "started", 0)), jid)
             for jid, j in JOBS.items() if getattr(j, "done", False)),
            reverse=True,
        )
        for _, jid in finished[MAX_FINISHED_JOBS:]:
            JOBS.pop(jid, None)


def start_job(kind: str, label: str, fn) -> Job:
    _prune_jobs()
    job = Job(kind, label)
    with LOCK:
        JOBS[job.id] = job

    def runner() -> None:
        try:
            result = fn(job.emit)
            if result is None:
                result = {}
            job.finish(result if isinstance(result, dict) else {"value": result})
        except SystemExit as exc:
            job.fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            job.fail(f"{type(exc).__name__}: {exc}")

    threading.Thread(target=runner, daemon=True).start()
    return job


# ---------------------------------------------------------------- 資料


def _fmt_ts(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _thumb_url(thumb: str | None, project: Path) -> str:
    """把縮圖路徑變成 /file/... 這種可以給 <img> 用的網址。"""
    if not thumb:
        return ""
    p = Path(thumb)
    if not p.exists():
        return ""
    try:
        rel = str(p.relative_to(project))
    except ValueError:
        return ""
    return f"/file/{urllib.parse.quote(project.name)}/{urllib.parse.quote(rel)}"


def clip_info(clip: dict[str, Any], project: Path, root: Path) -> dict[str, Any]:
    path = Path(clip.get("path", ""))
    try:
        rel = str(path.relative_to(project))
    except ValueError:
        rel = str(path)
    # 整支下載時 start/end 是 None（不是「沒有這個 key」），要當成整支
    start = clip.get("start")
    end = clip.get("end")
    is_full = start is None and end is None
    return {
        "name": path.name,
        "rel": f"/file/{urllib.parse.quote(str(project.name))}/{urllib.parse.quote(rel)}",
        "url": str(path),
        "start": 0 if start is None else start,
        "end": 0 if end is None else end,
        "full": is_full,
        "span": "整支" if is_full else f"{_fmt_ts(start)}–{_fmt_ts(end)}",
        "size": human_size(clip.get("size_bytes") or 0),
        "dims": f"{clip.get('width')}×{clip.get('height')}" if clip.get("width") else "",
        # 片段自己的縮圖（從這個片段檔抽的，畫面一定對得上）
        "thumb": _thumb_url(clip.get("thumb"), project),
        "orientation": clip.get("orientation") or "",
    }


def item_payload(item: Item, index: int, project: Path) -> dict[str, Any]:
    thumbs = []
    for f in item.thumbs:
        p = Path(f)
        if p.exists():
            try:
                rel = str(p.relative_to(project))
            except ValueError:
                rel = p.name
            thumbs.append(f"/file/{urllib.parse.quote(project.name)}/{urllib.parse.quote(rel)}")
    return {
        "index": index,
        "platform": item.platform,
        "platform_label": PLATFORM_LABEL.get(item.platform, item.platform),
        "vid": item.vid,
        "title": item.title,
        "author": item.author,
        "duration": fmt_duration(item.duration),
        "duration_sec": item.duration or 0,
        "views": item.views,
        "views_label": (f"{item.views / 10000:.1f}萬" if (item.views or 0) >= 10000 else
                        (str(item.views) if item.views is not None else "—")),
        "pubdate": item.pubdate,
        "url": item.url,
        "license_kind": item.license_kind,
        "license_label": item.license_label or "未標示授權",
        "license_short": LICENSE_SHORT.get(item.license_kind, "未知"),
        "repost": item.repost,
        "error": item.error,
        "note": item.check_note,
        "keyword": item.keyword,
        "thumbs": thumbs,
        "clips": [clip_info(c, project, project) for c in item.clips],
    }


def project_payload(project: Path) -> dict[str, Any]:
    manifest, items = tasks.load_project(project)
    stats = report.summarize(items)
    return {
        "name": project.name,
        "path": str(project),
        "queries": manifest.get("queries") or [],
        "sources": manifest.get("sources") or [],
        "created": manifest.get("created") or "",
        "updated": manifest.get("updated") or "",
        "stats": stats,
        "clips_bytes": human_size(stats["clip_bytes"]),
        "items": [item_payload(i, n, project) for n, i in enumerate(items)],
    }


def projects_payload() -> list[dict[str, Any]]:
    """回傳素材庫清單：已套用改名、排序與釘選。"""
    from . import library

    root = tasks.projects_root()
    folders = sorted([p.name for p in root.glob("*") if (p / "manifest.json").exists()])

    lib = library.load()
    lib, changed = library.sync(lib, folders)

    stats: dict[str, dict[str, Any]] = {}
    for p in [root / f for f in folders]:
        manifest, items = tasks.load_project(p)
        stats[p.name] = {
            "queries": manifest.get("queries") or [],
            "count": len(items),
            "clips": sum(len(i.clips) for i in items),
            "created": manifest.get("created") or "",
            "bytes": sum(
                int(c.get("size_bytes") or 0) for i in items for c in (i.clips or [])
            ),
        }
        # 順手記下「最後一次看到的樣子」，資料夾遺失時卡片才有內容可顯示
        rec = lib["items"].setdefault(p.name, {})
        if rec.get("count") != stats[p.name]["count"] or not rec.get("last_seen"):
            rec["count"] = stats[p.name]["count"]
            rec["clips"] = stats[p.name]["clips"]
            rec["queries"] = stats[p.name]["queries"]
            rec["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            changed = True

    if changed:
        library.save(lib)

    out = []
    for folder in lib.get("order", []):
        meta = library.entry(lib, folder)
        if folder not in stats:
            # 資料夾不見了：保留卡片，用記錄裡上次看到的樣子顯示
            if not meta["missing"]:
                continue
            out.append({
                "name": folder,
                "title": meta["title"],
                "pinned": meta["pinned"],
                "note": meta["note"],
                "missing": True,
                "queries": meta.get("queries") or [],
                "count": meta.get("count") or 0,
                "clips": meta.get("clips") or 0,
                "created": "",
                "size": "",
            })
            continue
        st = stats[folder]
        out.append({
            "name": folder,
            "title": meta["title"],
            "pinned": meta["pinned"],
            "note": meta["note"],
            "missing": meta["missing"],
            "queries": st["queries"],
            "count": st["count"],
            "clips": st["clips"],
            "created": st["created"],
            "size": human_size(st["bytes"]) if st["bytes"] else "",
        })
    return out




def resolve_pick_options(body: dict, saved: dict) -> dict[str, Any]:
    """把 API 請求與設定檔合成 run_pick 的參數。

    刻意抽成獨立函式：body 沒給的欄位一律回退到設定值，
    這樣「設定一次就變預設」才有單一正確來源，也才測得動。
    """
    from . import config as cfg

    def pick(key: str) -> Any:
        return body.get(key) if body.get(key) is not None else saved.get(key, cfg.DEFAULTS[key])

    return {
        "per": float(pick("per")),
        "starts": [float(s) for s in (body.get("starts") or [])],
        "ranges": [tuple(float(x) for x in pair) for pair in (body.get("ranges") or [])],
        "full": bool(body.get("full")),
        "frames": bool(pick("frames")),
        "frame_count": int(pick("frame_count")),
        "height": int(pick("height")),
        "audio": bool(pick("audio")),
        "audio_format": str(pick("audio_format")),
        "frame_only": bool(body.get("frame_only")),
        "cookies_from_browser": body.get("cookies_from_browser") or None,
    }


# ---------------------------------------------------------------- OpenAPI

OPENAPI: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {
        "title": "vidgrab API",
        "version": __version__,
        "description": (
            "關鍵字找素材：搜 B 站／YouTube，整支或切片下載。\n\n"
            "長時間的工作（搜尋、下載）是非同步的：先 POST 取得 job id，"
            "再 GET /api/job/{id} 輪詢，或訂閱 /api/stream/{id} 的 SSE 即時進度。"
        ),
    },
    "servers": [{"url": "http://127.0.0.1:8310"}],
    "tags": [
        {"name": "meta", "description": "版本與設定"},
        {"name": "search", "description": "搜尋"},
        {"name": "download", "description": "下載與切片"},
        {"name": "project", "description": "素材專案"},
        {"name": "auth", "description": "B 站登入"},
        {"name": "job", "description": "非同步工作"},
    ],
    "paths": {
        "/api/version": {
            "get": {"tags": ["meta"], "summary": "版本與 build number",
                    "responses": {"200": {"description": "OK"}}},
        },
        "/api/openapi.json": {
            "get": {"tags": ["meta"], "summary": "這份 OpenAPI 規格",
                    "responses": {"200": {"description": "OpenAPI 3.0"}}},
        },
        "/file/{project}/{path}": {
            "get": {
                "tags": ["project"], "summary": "取回片段檔或縮圖（支援 HTTP Range）",
                "parameters": [
                    {"name": "project", "in": "path", "required": True,
                     "schema": {"type": "string"}},
                    {"name": "path", "in": "path", "required": True,
                     "schema": {"type": "string"}, "description": "例如 clips/xxx.mp4"},
                ],
                "responses": {"200": {"description": "檔案內容"},
                              "404": {"description": "找不到"}},
            },
        },
        "/api/config": {
            "get": {"tags": ["meta"], "summary": "讀設定（含預設值）",
                    "responses": {"200": {"description": "OK"}}},
            "post": {
                "tags": ["meta"], "summary": "改設定，存過就成為預設",
                "requestBody": {
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "example": {"set": {"per": 30, "height": 720, "platform": "bilibili"}},
                    }}}
                },
                "responses": {"200": {"description": "已更新"}, "400": {"description": "值不合法"}},
            },
        },
        "/api/search": {
            "post": {
                "tags": ["search"], "summary": "搜尋並建立專案（非同步）",
                "requestBody": {
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "required": ["keywords"],
                        "properties": {
                            "keywords": {"type": "array", "items": {"type": "string"},
                                         "example": ["台北 街景", "北歐 空景"]},
                            "platform": {"type": "string", "enum": ["both", "bilibili", "youtube"]},
                            "per_keyword": {"type": "integer"},
                            "order": {"type": "string", "enum": ["smart", "relevance", "views", "new"]},
                            "min_duration": {"type": "number", "description": "分鐘"},
                            "max_duration": {"type": "number", "description": "分鐘，0=不限"},
                            "fresh_days": {"type": "integer"},
                            "min_views": {"type": "integer"},
                            "exclude": {"type": "array", "items": {"type": "string"}},
                            "only_free": {"type": "boolean"},
                            "drop_reposts": {"type": "boolean"},
                            "verify_license": {"type": "boolean"},
                            "sort": {"type": "string", "enum": ["smart", "license", "views", "new"]},
                            "name": {"type": "string"},
                            "save_settings": {"type": "boolean",
                                              "description": "把這次的參數存成預設"},
                        },
                    }}}
                },
                "responses": {"200": {"description": "回傳 {job: id}"}},
            },
        },
        "/api/pick": {
            "post": {
                "tags": ["download"], "summary": "下載整支或切片（非同步）",
                "requestBody": {
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "required": ["project", "indexes"],
                        "properties": {
                            "project": {"type": "string"},
                            "indexes": {"type": "array", "items": {"type": "integer"},
                                        "description": "0 起算的候選編號"},
                            "full": {"type": "boolean", "description": "整支下載"},
                            "per": {"type": "number", "description": "每段秒數"},
                            "starts": {"type": "array", "items": {"type": "number"}},
                            "ranges": {"type": "array", "items": {
                                "type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}},
                            "height": {"type": "integer"},
                            "audio": {"type": "boolean"},
                            "frames": {"type": "boolean"},
                            "frame_only": {"type": "boolean"},
                            "save_settings": {"type": "boolean"},
                        },
                    }}}
                },
                "responses": {"200": {"description": "回傳 {job: id}"}},
            },
        },
        "/api/job/{id}": {
            "get": {"tags": ["job"], "summary": "查工作狀態與完整訊息",
                    "parameters": [{"name": "id", "in": "path", "required": True,
                                    "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "OK"}}},
        },
        "/api/stream/{id}": {
            "get": {"tags": ["job"], "summary": "SSE 即時進度（log / done / error）",
                    "parameters": [{"name": "id", "in": "path", "required": True,
                                    "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "text/event-stream"}}},
        },
        "/api/projects": {
            "get": {"tags": ["project"], "summary": "列出所有專案",
                    "responses": {"200": {"description": "OK"}}},
        },
        "/api/project/{name}": {
            "get": {"tags": ["project"], "summary": "單一專案完整內容（含候選與已下載片段）",
                    "parameters": [{"name": "name", "in": "path", "required": True,
                                    "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "OK"}}},
        },
        "/api/item/{name}/{index}": {
            "get": {"tags": ["project"], "summary": "單一候選影片",
                    "parameters": [
                        {"name": "name", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "index", "in": "path", "required": True, "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"description": "OK"}}},
        },
        "/api/library/scan": {
            "get": {"tags": ["project"], "summary": "掃描 projects/，找出未納入與遺失的專案",
                    "responses": {"200": {"description": "OK"}}},
        },
        "/api/asset/{project}/{index}": {
            "get": {
                "tags": ["project"],
                "summary": "單支素材的完整 metadata（標籤、可聯繫資訊、標註建議、片段）",
                "parameters": [
                    {"name": "project", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "index", "in": "path", "required": True, "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "OK"}, "404": {"description": "找不到"}},
            },
        },
        "/api/library": {
            "get": {"tags": ["project"], "summary": "素材庫（含改名、釘選、目前排序）",
                    "responses": {"200": {"description": "OK"}}},
            "post": {
                "tags": ["project"],
                "summary": "改名／釘選／排序／拖曳",
                "requestBody": {
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "required": ["action"],
                        "properties": {
                            "action": {"type": "string",
                                       "enum": ["rename", "pin", "order", "move", "sort",
                                                "adopt", "forget"]},
                            "project": {"type": "string", "description": "資料夾名"},
                            "title": {"type": "string", "description": "rename 用"},
                            "pinned": {"type": "boolean", "description": "pin 用"},
                            "order": {"type": "array", "items": {"type": "string"}},
                            "folders": {"type": "array", "items": {"type": "string"},
                                        "description": "adopt／forget 用"},
                            "delta": {"type": "integer", "description": "move 用，-1 上移、1 下移"},
                            "mode": {"type": "string",
                                     "enum": ["manual", "created", "name", "clips", "count"]},
                        },
                    }}}
                },
                "responses": {"200": {"description": "回傳更新後的清單"},
                              "400": {"description": "參數不合法"}},
            },
        },
        "/api/project/delete": {
            "post": {"tags": ["project"], "summary": "刪除整個專案（連同已下載檔案）",
                     "responses": {"200": {"description": "OK"}}},
        },
        "/api/delete-clip": {
            "post": {"tags": ["project"], "summary": "刪除單一片段檔",
                     "responses": {"200": {"description": "OK"}}},
        },
        "/api/refresh": {
            "post": {"tags": ["project"], "summary": "重新確認授權與影片是否還在（非同步）",
                     "responses": {"200": {"description": "回傳 {job: id}"}}},
        },
        "/api/export": {
            "get": {"tags": ["project"], "summary": "匯出全部設定與專案索引（備份用）",
                    "responses": {"200": {"description": "OK"}}},
        },
        "/api/login/start": {
            "post": {"tags": ["auth"], "summary": "產生 B 站登入 QR",
                     "responses": {"200": {"description": "回傳 {key, image}"}}},
        },
        "/api/login/qr": {
            "get": {"tags": ["auth"], "summary": "QR 圖（PNG）",
                    "responses": {"200": {"description": "image/png"}}},
        },
        "/api/login/poll": {
            "post": {"tags": ["auth"], "summary": "查掃碼結果",
                     "responses": {"200": {"description": "status: waiting/scanned/ok/expired"}}},
        },
        "/api/login/status": {
            "get": {"tags": ["auth"], "summary": "目前登入的帳號",
                    "responses": {"200": {"description": "OK"}}},
        },
        "/api/login/logout": {
            "post": {"tags": ["auth"], "summary": "登出並刪除 cookie",
                     "responses": {"200": {"description": "OK"}}},
        },
        "/api/open": {
            "post": {"tags": ["project"], "summary": "用瀏覽器打開 HTML 清單",
                     "responses": {"200": {"description": "OK"}}},
        },
    },
}


# ---------------------------------------------------------------- 頁面


class Handler(BaseHTTPRequestHandler):
    server_version = "vidgrab/0.1"

    def log_message(self, fmt: str, *args) -> None:  # 安靜一點
        return

    # -- 工具 ---------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(_jsonable(data), ensure_ascii=False).encode())

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _project_dir(self, name: str) -> Path | None:
        name = urllib.parse.unquote(name)
        path = (tasks.projects_root() / name).resolve()
        root = tasks.projects_root().resolve()
        if not str(path).startswith(str(root)) or not (path / "manifest.json").exists():
            return None
        return path

    # -- 路由 ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            self._send(200, render_page().encode(), "text/html; charset=utf-8")
            return
        if route == "/api/projects":
            self._json(projects_payload())
            return
        if route.startswith("/api/project/"):
            project = self._project_dir(route.split("/api/project/", 1)[1])
            if not project:
                self._json({"error": "找不到專案"}, 404)
            else:
                self._json(project_payload(project))
            return
        if route.startswith("/api/job/"):
            job = JOBS.get(route.rsplit("/", 1)[-1])
            if not job:
                self._json({"error": "找不到工作"}, 404)
            else:
                self._json(job.snapshot())
            return
        if route.startswith("/api/stream/"):
            self._stream(route.rsplit("/", 1)[-1])
            return
        if route == "/api/login/qr":
            from . import auth

            png = auth.home() / "bilibili-qr.png"
            if not png.exists():
                self._json({"error": "沒有 QR"}, 404)
                return
            self._send(200, png.read_bytes(), "image/png")
            return
        if route.startswith("/api/asset/"):
            # 單支素材的完整資訊，給外部工具（剪輯軟體、素材管理）讀取
            from .models import contact_block, search_terms

            parts = route.split("/api/asset/", 1)[1].split("/")
            project = self._project_dir(urllib.parse.unquote(parts[0]))
            if not project:
                self._json({"error": "找不到專案"}, 404)
                return
            try:
                idx = int(parts[1]) if len(parts) > 1 else -1
            except ValueError:
                self._json({"error": "索引必須是整數"}, 400)
                return
            _, items = tasks.load_project(project)
            if not (0 <= idx < len(items)):
                self._json({"error": f"索引超出範圍（0–{len(items) - 1}）"}, 404)
                return
            it = items[idx]
            self._json({
                "index": idx,
                "platform": it.platform,
                "vid": it.vid,
                "title": it.title,
                "author": it.author,
                "channel": it.channel,
                "author_url": it.author_url,
                "webpage_url": it.webpage_url or it.url,
                "thumbnail": it.thumbnail,
                "duration": it.duration,
                "width": it.width,
                "height": it.height,
                "fps": it.fps,
                "views": it.views,
                "pubdate": it.pubdate,
                "language": it.language,
                "tags": it.tags,
                "categories": it.categories,
                "search_terms": search_terms(it),
                "description": it.description_full or it.description,
                "license": {
                    "kind": it.license_kind,
                    "label": it.license_label,
                    "name": it.license_name,
                },
                "repost": it.repost,
                "contact": contact_block(it),
                "clips": [
                    {
                        "path": c.get("path"),
                        "start": c.get("start"),
                        "end": c.get("end"),
                        "duration": c.get("duration"),
                        "width": c.get("width"),
                        "height": c.get("height"),
                        "size_bytes": c.get("size_bytes"),
                        "url": "/file/" + urllib.parse.quote(
                            str(Path(c.get("path", "")).relative_to(tasks.projects_root()))
                        ) if c.get("path") else "",
                    }
                    for c in it.clips
                ],
            })
            return
        if route == "/api/library/scan":
            from . import library

            self._json(library.scan())
            return
        if route == "/api/library":
            from . import library

            lib = library.load()
            self._json({
                "projects": projects_payload(),
                "order": lib.get("order", []),
                "sort": lib.get("sort", "manual"),
            })
            return
        if route == "/api/config":
            from . import config as cfg

            self._json(cfg.describe())
            return
        if route == "/api/openapi.json":
            self._send(200, json.dumps(OPENAPI, ensure_ascii=False, indent=2).encode())
            return
        if route == "/api/export":
            from . import auth as _auth

            self._json({
                "version": __version__,
                "config": __import__("vidgrab.config", fromlist=["load"]).load(),
                "logged_in": _auth.logged_in(),
                "projects": projects_payload(),
            })
            return
        if route.startswith("/api/item/"):
            parts = route[len("/api/item/"):].split("/", 1)
            project = self._project_dir(parts[0])
            if not project or len(parts) < 2:
                self._json({"error": "找不到專案或編號"}, 404)
                return
            try:
                idx = int(parts[1])
            except ValueError:
                self._json({"error": "編號要是整數"}, 400)
                return
            manifest, items = tasks.load_project(project)
            if not (0 <= idx < len(items)):
                self._json({"error": f"編號超出範圍（0–{len(items) - 1}）"}, 404)
                return
            self._json(item_payload(items[idx], idx, project))
            return
        if route == "/api/version":
            from .version import build, full_version

            self._json({"version": __version__, "build": build(), "full": full_version()})
            return
        if route == "/api/login/status":
            from . import auth

            self._json(auth.whoami())
            return
        if route.startswith("/static/"):
            self._static(route[len("/static/"):])
            return
        if route.startswith("/file/"):
            self._file(route[len("/file/"):])
            return
        if route == "/api/report/":
            self._json({"error": "缺少專案"}, 404)
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _origin_ok(self) -> bool:
        """擋掉跨站請求（CSRF）。

        這個服務沒有登入機制，所以「瀏覽器幫你送出的請求」跟「你自己按的」
        分不出來。做法是只認本機來源：
        - 沒有 Origin／Referer（curl、本機程式）→ 放行
        - 有，就必須是 127.0.0.1 / localhost / ::1 的位址
        """
        for header in ("Origin", "Referer"):
            value = self.headers.get(header)
            if not value:
                continue
            try:
                host = urllib.parse.urlparse(value).hostname or ""
            except ValueError:
                return False
            if host in ("127.0.0.1", "localhost", "::1", "[::1]"):
                return True
            return False
        return True

    def do_POST(self) -> None:  # noqa: N802
        route = urllib.parse.urlparse(self.path).path

        if not self._origin_ok():
            self._json({"error": "跨站請求已被拒絕"}, 403)
            return

        body = self._body()

        if route == "/api/library":
            from . import library

            if not body:
                self._json({"projects": projects_payload(), "order": library.load().get("order", [])})
                return
            action = body.get("action")
            lib = library.load()
            try:
                if action == "rename":
                    folder = body.get("project", "")
                    if not self._project_dir(folder):
                        self._json({"error": "找不到專案"}, 404)
                        return
                    library.rename(lib, folder, body.get("title", ""))
                elif action == "pin":
                    folder = body.get("project", "")
                    if not self._project_dir(folder):
                        self._json({"error": "找不到專案"}, 404)
                        return
                    library.set_pinned(lib, folder, bool(body.get("pinned")))
                elif action == "order":
                    library.set_order(lib, body.get("order") or [])
                elif action == "move":
                    library.move(lib, body.get("project", ""), int(body.get("delta") or 0))
                elif action == "sort":
                    stats = {p["name"]: p for p in projects_payload()}
                    library.sort_by(lib, body.get("mode", "manual"), stats)
                elif action == "adopt":
                    folders = body.get("folders") or []
                    library.adopt(lib, [f for f in folders if self._project_dir(f)])
                elif action == "forget":
                    # 只移除「記錄」，不動磁碟上的資料夾
                    library.forget(lib, body.get("folders") or [])
                else:
                    self._json({"error": f"不認得的動作：{action}"}, 400)
                    return
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            library.save(lib)
            self._json({"ok": True, "projects": projects_payload()})
            return

        if route == "/api/config":
            from . import config as cfg

            try:
                if body.get("reset") is not None:
                    keys = body.get("reset") or None
                    values = cfg.reset(keys if isinstance(keys, list) else None)
                else:
                    patch = body.get("set") or body
                    values = cfg.save_patch({k: v for k, v in patch.items() if k in cfg.DEFAULTS})
            except cfg.ConfigError as exc:
                self._json({"error": str(exc)}, 400)
                return
            self._json({"ok": True, "values": values, "path": str(cfg.config_path())})
            return

        if route == "/api/login/logout":
            from . import auth

            auth.logout()
            self._json({"ok": True})
            return

        if route == "/api/project/delete":
            name = body.get("project", "")
            project = self._project_dir(name)
            if not project:
                self._json({"error": "找不到專案"}, 404)
                return
            import shutil

            shutil.rmtree(project)
            self._json({"ok": True, "deleted": name})
            return

        if route == "/api/search":
            queries = [q for q in (body.get("keywords") or []) if str(q).strip()]
            if not queries:
                self._json({"error": "請至少輸入一個關鍵字"}, 400)
                return
            from . import config as cfg

            saved = cfg.load()
            def pick(key, cast=None):
                if body.get(key) is not None:
                    return body[key]
                value = saved.get(key, cfg.DEFAULTS.get(key))
                return cast(value) if cast and value is not None else value

            opts = {
                "platform": pick("platform"),
                "per_keyword": int(pick("per_keyword")),
                "order": pick("order"),
                "duration": pick("duration"),
                "exclude": body.get("exclude") or saved.get("exclude") or [],
                "fresh_days": int(pick("fresh_days")),
                "min_duration": float(pick("min_duration")),
                "max_duration": float(pick("max_duration")),
                "min_views": int(pick("min_views")),
                "only_free": bool(pick("only_free")),
                "drop_reposts": bool(pick("drop_reposts")),
                "verify_license": bool(pick("verify_license")),
                "sort": pick("sort"),
                "name": body.get("name") or None,
            }
            if body.get("save_settings"):
                patch = {k: opts[k] for k in opts if k in cfg.DEFAULTS}
                cfg.save_patch(patch)

            def work(emit):
                res = tasks.run_search(queries, emit=emit, **opts)
                return {"project": res["project"].name, "count": len(res["items"])}

            job = start_job("search", "、".join(queries), work)
            self._json({"job": job.id})
            return

        if route == "/api/pick":
            project = self._project_dir(body.get("project", ""))
            if not project:
                self._json({"error": "找不到專案"}, 404)
                return
            indexes = [int(i) for i in (body.get("indexes") or [])]
            if not indexes:
                self._json({"error": "請先勾選影片"}, 400)
            from . import config as cfg

            opts = resolve_pick_options(body, cfg.load())
            if body.get("save_settings"):
                cfg.save_patch({
                    "per": opts["per"],
                    "height": opts["height"],
                    "audio": opts["audio"],
                    "frames": opts["frames"],
                    "frame_count": opts["frame_count"],
                    "audio_format": opts["audio_format"],
                })

            def work(emit):
                res = tasks.run_pick(project, indexes, emit=emit, **opts)
                return {"project": project.name, "grabbed": res["grabbed"], "count": len(indexes)}

            job = start_job("pick", f"{project.name} · {len(indexes)} 支", work)
            self._json({"job": job.id})
            return

        if route == "/api/refresh":
            project = self._project_dir(body.get("project", ""))
            if not project:
                self._json({"error": "找不到專案"}, 404)
                return

            def work(emit):
                manifest, items = tasks.load_project(project)
                from .sources import get_sources

                sources = {s.name: s for s in get_sources("both")}
                for item in items:
                    src = sources.get(item.platform)
                    if not src:
                        continue
                    src.detail(item)
                    emit(f"{item.vid} → {item.license_label or '未標示'}{'（轉載）' if item.repost else ''}")
                tasks.save_project(project, manifest, items)
                return {"project": project.name, "count": len(items)}

            job = start_job("refresh", project.name, work)
            self._json({"job": job.id})
            return

        if route == "/api/login/poll":
            from . import auth

            key = body.get("key", "")
            if not key:
                self._json({"error": "缺少 key"}, 400)
                return
            res = auth.poll(key)
            self._json(res)
            return

        if route.startswith("/api/delete-clip"):
            try:
                target = Path(body.get("path", "")).resolve()
                root = tasks.projects_root().resolve()
                if not str(target).startswith(str(root)) or not target.exists():
                    raise ValueError("不合法路徑")
                project = target.parents[1]
                target.unlink()
                manifest, items = tasks.load_project(project)
                for item in items:
                    item.clips = [c for c in item.clips if Path(c.get("path", "")).name != target.name]
                tasks.save_project(project, manifest, items)
                self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
            return

        if route == "/api/login/start":
            from . import auth

            if auth.logged_in():
                who = auth.whoami()
                if who.get("logged_in"):
                    self._json({"already": who})
                    return
            qr = auth.generate()
            png = auth.home() / "bilibili-qr.png"
            png.write_bytes(qr["png"])
            with LOCK:
                QR[f"{qr['key']}"] = time.time()
            self._json({"key": qr["key"], "image": "/api/login/qr", "expires_in": 180})
            return

        if route == "/api/open":
            project = self._project_dir(body.get("project", ""))
            if project:
                webbrowser.open(f"file://{project / 'index.html'}")
            self._json({"ok": True})
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")

    # -- SSE ----------------------------------------------------------

    def _stream(self, job_id: str) -> None:
        job = JOBS.get(job_id)
        if not job:
            self._json({"error": "找不到工作"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for message in job.messages:
                self._log_event(message)
            # 歷史已經重播過，佇列裡同一批 log 丟掉，只留結尾事件
            while True:
                try:
                    kind, payload = job.queue.get_nowait()
                except queue.Empty:
                    break
                if kind in ("done", "error"):
                    self._event(kind, payload)
                    return
            while True:
                try:
                    kind, payload = job.queue.get(timeout=5)
                except queue.Empty:
                    # 客戶端可能早就斷了。只寫 ping 的話，
                    # 要等到下一次 flush 失敗才發現（原本是 20 秒一輪）。
                    # 先用 select 確認連線還在，斷了就立刻收工。
                    if self._peer_gone():
                        return
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if kind == "log":
                    self._log_event(payload)
                else:
                    self._event(kind, payload)
                if kind in ("done", "error"):
                    break
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def _peer_gone(self) -> bool:
        """連線的另一端還在嗎？（不回傳資料，只問 socket 狀態）"""
        try:
            import select

            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            # 可讀但沒有資料 → 對方關閉了
            data = self.connection.recv(1, socket.MSG_PEEK)
            return data == b""
        except (OSError, ValueError):
            return True

    def _event(self, kind: str, payload: Any) -> None:
        data = json.dumps(_jsonable(payload), ensure_ascii=False)
        self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode())
        self.wfile.flush()

    def _log_event(self, message: Any) -> None:
        """送一則日誌。

        進度行是「<給人看的>\x00<JSON>」：人話走 log 事件，
        JSON 另外包成 progress 事件。前端才不必自己去切字串 ——
        以前沒切，log 視窗裡直接印出整包 {"kind": "download", ...}。
        """
        text = str(message)
        spoken, _, raw = text.partition("\x00")
        self._event("log", spoken.lstrip("\r"))
        if not raw:
            return
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(payload, dict):
            self._event("progress", payload)

    # -- 檔案 ---------------------------------------------------------

    def _static(self, rest: str) -> None:
        """送打包好的前端檔案（CSS／JS／外殼）。

        只認 STATIC_DIR 底下的檔案；用 relative_to 而不是字串前綴，
        才不會被 '../' 之類的路徑穿出去。
        """
        rel = urllib.parse.unquote(rest)
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except (ValueError, OSError):
            self._send(404, b"not found", "text/plain")
            return
        if not target.is_file():
            self._send(404, b"not found", "text/plain")
            return

        data = target.read_bytes()
        ctype = _STATIC_TYPES.get(target.suffix.lower(), "application/octet-stream")
        # 開發時不方便快取，但檔名固定，所以用 no-cache + ETag 讓瀏覽器每次驗證
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _file(self, rest: str) -> None:
        parts = rest.split("/", 1)
        if len(parts) != 2:
            self._send(404, b"not found", "text/plain")
            return
        project_name, rel = urllib.parse.unquote(parts[0]), urllib.parse.unquote(parts[1])
        project = tasks.projects_root() / project_name
        target = (project / rel).resolve()
        # 不能用 str.startswith 判斷！那是路徑穿越漏洞：
        #   /projects/victim  vs  /projects/victim-evil/secret.txt
        #   字串前綴成立，於是 `victim` 這個專案可以讀到 `victim-evil` 的檔案。
        # 用 relative_to 才是真的在比目錄層級。
        try:
            base = project.resolve()
            target.relative_to(base)
        except (ValueError, OSError):
            self._send(404, b"not found", "text/plain")
            return
        if not target.exists() or target.is_dir():
            self._send(404, b"not found", "text/plain")
            return
        ctype = {
            ".mp4": "video/mp4", ".m4a": "audio/mp4", ".mp3": "audio/mpeg",
            ".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
            ".webm": "video/webm", ".html": "text/html; charset=utf-8",
        }.get(target.suffix.lower(), "application/octet-stream")

        size = target.stat().st_size
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        if range_header and range_header.startswith("bytes="):
            spec = range_header.split("=", 1)[1].split(",")[0]
            lo, _, hi = spec.partition("-")
            try:
                if lo:
                    start = int(lo)
                if hi:
                    end = int(hi)
            except ValueError:
                start, end = 0, size - 1
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with target.open("rb") as fh:
                fh.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve(host: str | None = None, port: int = 8310, open_browser: bool = True) -> None:
    import os

    host = host or os.environ.get("VIDGRAB_HOST") or "127.0.0.1"
    root = tasks.projects_root()
    root.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"▸ vidgrab Web UI：{url}")
    print(f"  素材專案目錄：{root}")
    print("  Ctrl+C 結束")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n關閉。")
    finally:
        httpd.server_close()


STATIC_DIR = Path(__file__).with_name("static")
_STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}


def render_page() -> str:
    """網頁外殼。內容全部走 /api/，所以這裡不需要插值任何資料。"""
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")



def main() -> None:  # pragma: no cover
    serve()
