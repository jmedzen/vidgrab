"""Web UI：本機網頁介面，用 SSE 即時回報搜尋／切片進度。"""

from __future__ import annotations

import html
import json
import queue
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

    def emit(self, message: str) -> None:
        self.messages.append(message)
        self.queue.put(("log", message))

    def finish(self, result: dict[str, Any]) -> None:
        self.done = True
        self.result = result
        self.queue.put(("done", result))

    def fail(self, message: str) -> None:
        self.done = True
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


def start_job(kind: str, label: str, fn) -> Job:
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


def clip_info(clip: dict[str, Any], project: Path, root: Path) -> dict[str, Any]:
    path = Path(clip.get("path", ""))
    rel = str(path.relative_to(project)) if str(path).startswith(str(project)) else str(path)
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
    }


def item_payload(item: Item, index: int, project: Path) -> dict[str, Any]:
    thumbs = []
    for f in item.thumbs:
        p = Path(f)
        if p.exists():
            rel = str(p.relative_to(project)) if str(p).startswith(str(project)) else p.name
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
    root = tasks.projects_root()
    out = []
    for p in sorted([p for p in root.glob("*") if (p / "manifest.json").exists()], reverse=True):
        manifest, items = tasks.load_project(p)
        out.append(
            {
                "name": p.name,
                "queries": manifest.get("queries") or [],
                "count": len(items),
                "clips": sum(len(i.clips) for i in items),
                "created": manifest.get("created") or "",
            }
        )
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


def render_page() -> str:
    return PAGE


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
        if route.startswith("/file/"):
            self._file(route[len("/file/"):])
            return
        if route == "/api/report/":
            self._json({"error": "缺少專案"}, 404)
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        route = urllib.parse.urlparse(self.path).path
        body = self._body()

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
                self._event("log", message)
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
                    kind, payload = job.queue.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._event(kind, payload)
                if kind in ("done", "error"):
                    break
        except (BrokenPipeError, ConnectionResetError):
            return

    def _event(self, kind: str, payload: Any) -> None:
        data = json.dumps(_jsonable(payload), ensure_ascii=False)
        self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode())
        self.wfile.flush()

    # -- 檔案 ---------------------------------------------------------

    def _file(self, rest: str) -> None:
        parts = rest.split("/", 1)
        if len(parts) != 2:
            self._send(404, b"not found", "text/plain")
            return
        project_name, rel = urllib.parse.unquote(parts[0]), urllib.parse.unquote(parts[1])
        project = tasks.projects_root() / project_name
        target = (project / rel).resolve()
        if not str(target).startswith(str(project.resolve())) or not target.exists() or target.is_dir():
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


PAGE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%230b0d12'/%3E%3Cpath d='M9 8h4l3 9 3-9h4l-5.5 16h-3z' fill='%236cc0ff'/%3E%3C/svg%3E">
<title>vidgrab · 素材獵人</title>
<script>
// 在第一次繪製之前就決定主題與側欄寬度，否則淺色主題會先閃一下深色
(function () {
  try {
    var t = localStorage.getItem('vidgrab.theme');
    if (!t && window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) t = 'paper';
    document.documentElement.dataset.theme = t || 'midnight';
    var w = Number(localStorage.getItem('vidgrab.sidebar'));
    if (w >= 240 && w <= 620 && window.innerWidth > 820) {
      document.documentElement.style.setProperty('--sidebar', w + 'px');
    }
  } catch (e) {}
})();
</script>
<style>

  /* ---------------------------------------------------------- 主題
     每個主題只換變數，版面不動。深淺各三組，都調過對比度。 */

  /* 預設：Midnight（深） */
  :root, [data-theme="midnight"] {
    --bg:#0b0d12; --panel:#141821; --panel2:#1a1f2b; --line:#262c3a;
    --text:#e9ecf3; --muted:#8e98ad; --accent:#6cc0ff; --ok:#5ee2a0;
    --warn:#f3c96b; --bad:#ff8b8b; --shadow:0 10px 30px rgba(0,0,0,.35);
    --aside-bg:rgba(16,20,28,.85); --code-bg:#0b0e14;
    --glow:radial-gradient(1200px 600px at 80% -10%, #17202e 0%, var(--bg) 55%);
    --card-bg:linear-gradient(180deg,#161b25 0%,#131820 100%);
    --accent-soft:rgba(108,192,255,.12); --scrim:rgba(0,0,0,.8);
    --on-accent:#06121c; --accent-hover:#8ed1ff;
  }

  /* 深 2：Ink（近黑，低飽和，適合長時間看） */
  [data-theme="ink"] {
    --bg:#08090b; --panel:#101114; --panel2:#16181d; --line:#232629;
    --text:#e6e6e4; --muted:#8a8d92; --accent:#c9a227; --ok:#79c39a;
    --warn:#d8b25a; --bad:#e08b7f; --shadow:0 10px 28px rgba(0,0,0,.55);
    --aside-bg:rgba(10,11,13,.9); --code-bg:#0c0d10;
    --glow:radial-gradient(1100px 560px at 78% -12%, #14161a 0%, var(--bg) 60%);
    --card-bg:linear-gradient(180deg,#121417 0%,#101215 100%);
    --accent-soft:rgba(201,162,39,.13); --scrim:rgba(0,0,0,.85);
    --on-accent:#14100a; --accent-hover:#dcb63f;
  }

  /* 深 3：Forest（墨綠，柔和） */
  [data-theme="forest"] {
    --bg:#0a1210; --panel:#0f1a17; --panel2:#13211d; --line:#1f3330;
    --text:#e4efe9; --muted:#87a29a; --accent:#6fd6a8; --ok:#6fd6a8;
    --warn:#e3c46b; --bad:#ef8f8f; --shadow:0 10px 30px rgba(0,0,0,.4);
    --aside-bg:rgba(10,18,16,.88); --code-bg:#0a1210;
    --glow:radial-gradient(1150px 580px at 80% -10%, #10241f 0%, var(--bg) 58%);
    --card-bg:linear-gradient(180deg,#10201c 0%,#0e1a17 100%);
    --accent-soft:rgba(111,214,168,.13); --scrim:rgba(0,0,0,.82);
    --on-accent:#06140f; --accent-hover:#8ee4bf;
  }

  /* 淺 1：Paper（米白，像紙） */
  [data-theme="paper"] {
    --bg:#f7f4ee; --panel:#fffdf9; --panel2:#f2eee6; --line:#e0d9cc;
    --text:#232019; --muted:#6f6759; --accent:#b5651d; --ok:#2f7d4f;
    --warn:#9a6b15; --bad:#b03a3a; --shadow:0 8px 24px rgba(80,60,30,.12);
    --aside-bg:rgba(255,253,249,.9); --code-bg:#f0ece3;
    --glow:radial-gradient(1200px 600px at 82% -14%, #fff9ee 0%, var(--bg) 60%);
    --card-bg:linear-gradient(180deg,#fffdf9 0%,#fbf8f1 100%);
    --accent-soft:rgba(181,101,29,.10); --scrim:rgba(60,45,25,.55);
    --on-accent:#fffaf2; --accent-hover:#9c5417;
  }

  /* 淺 2：Mist（冷灰藍，乾淨） */
  [data-theme="mist"] {
    --bg:#eef2f7; --panel:#ffffff; --panel2:#f1f5fa; --line:#d6dee8;
    --text:#1b2532; --muted:#68758a; --accent:#2f6fd0; --ok:#1f7a52;
    --warn:#8a6512; --bad:#b23b4a; --shadow:0 8px 24px rgba(30,50,80,.10);
    --aside-bg:rgba(255,255,255,.92); --code-bg:#eef3f9;
    --glow:radial-gradient(1200px 620px at 80% -12%, #ffffff 0%, var(--bg) 62%);
    --card-bg:linear-gradient(180deg,#ffffff 0%,#fafcff 100%);
    --accent-soft:rgba(47,111,208,.10); --scrim:rgba(20,35,60,.55);
    --on-accent:#ffffff; --accent-hover:#245ab0;
  }

  /* 淺 3：Sakura（暖粉，柔） */
  [data-theme="sakura"] {
    --bg:#fdf4f5; --panel:#fffafb; --panel2:#fbeef0; --line:#eed9de;
    --text:#2b1f24; --muted:#7d626b; --accent:#c25a7a; --ok:#3d7f5e;
    --warn:#96701c; --bad:#bb3b57; --shadow:0 8px 24px rgba(120,60,80,.12);
    --aside-bg:rgba(255,250,251,.92); --code-bg:#f8ecef;
    --glow:radial-gradient(1200px 600px at 82% -12%, #fff7f9 0%, var(--bg) 62%);
    --card-bg:linear-gradient(180deg,#fffafb 0%,#fdf3f5 100%);
    --accent-soft:rgba(194,90,122,.10); --scrim:rgba(70,35,48,.55);
    --on-accent:#fff7f9; --accent-hover:#a84a66;
  }

  * { box-sizing:border-box; }
  .brandrow { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }
  .theme-picker { position:relative; flex:none; }
  .theme-btn {
    width:32px; height:32px; padding:0; display:grid; place-items:center;
    font-size:15px; line-height:1; border-radius:9px;
  }
  .theme-menu {
    position:absolute; right:0; top:38px; z-index:60; width:206px; display:none;
    background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:6px; box-shadow:var(--shadow);
  }
  .theme-menu.open { display:block; }
  .theme-menu .group {
    font-size:10.5px; letter-spacing:.12em; text-transform:uppercase;
    color:var(--muted); padding:7px 9px 4px;
  }
  .theme-menu button {
    width:100%; display:flex; align-items:center; gap:9px; justify-content:flex-start;
    background:transparent; border:none; padding:7px 9px; border-radius:8px;
    color:var(--text); font-size:13px;
  }
  .theme-menu button:hover { background:var(--panel2); border:none; }
  .theme-menu button[aria-checked="true"] { background:var(--accent-soft); }
  .swatch { width:14px; height:14px; border-radius:50%; flex:none; border:1px solid rgba(128,128,128,.35); }
  html,body { height:100%; }
  body {
    margin:0; background:var(--glow);
    color:var(--text); font:14.5px/1.6 -apple-system,"PingFang TC","Noto Sans TC",system-ui,sans-serif;
  }
  .app {
    display:grid; grid-template-columns:var(--sidebar, 330px) 1fr;
    height:100vh; min-height:0;
  }
  aside {
    position:relative; border-right:1px solid var(--line); background:var(--aside-bg);
    padding:18px 16px; overflow-y:auto; overflow-x:hidden; min-width:0;
    box-sizing:border-box;
  }
  aside * { max-width:100%; box-sizing:border-box; }
  /* 拖曳把手：滑鼠移到邊界就出現 */
  .resizer {
    position:absolute; top:0; right:0; width:6px; height:100%;
    cursor:col-resize; z-index:20; background:transparent; transition:background .15s;
    touch-action:none;
  }
  .resizer:hover, .resizer.dragging { background:var(--accent); opacity:.55; }
  body.resizing { cursor:col-resize; user-select:none; }
  body.resizing iframe, body.resizing video { pointer-events:none; }
  .resizer:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
  @media (max-width:820px) {
    .app { grid-template-columns:1fr; grid-template-rows:auto 1fr; }
    aside { border-right:none; border-bottom:1px solid var(--line); max-height:42vh; }
    .resizer { display:none; }
  }
  main { overflow-y:auto; padding:22px 26px 80px; }
  h1 { font-size:19px; margin:0 0 2px; letter-spacing:.04em; }
  h1 span { color:var(--accent); }
  .tagline { color:var(--muted); font-size:12px; margin-bottom:4px; }
  .buildtag { color:var(--muted); opacity:.72; font-size:11px; margin-bottom:16px; font-variant-numeric:tabular-nums; }
  label.field { display:block; margin-bottom:12px; }
  label.field > span { display:block; font-size:12px; color:var(--muted); margin-bottom:5px; }
  input[type=text], input[type=number], select, textarea {
    width:100%; background:var(--panel2); border:1px solid var(--line); color:var(--text);
    border-radius:9px; padding:8px 10px; font:inherit; font-size:13.5px; outline:none;
  }
  input:focus, select:focus, textarea:focus { border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
  textarea { resize:vertical; min-height:64px; }
  .row { display:flex; gap:8px; }
  .row > * { flex:1; }
  .checks { display:grid; gap:6px; margin:10px 0 14px; font-size:13px; color:var(--muted); }
  .checks label { display:flex; gap:8px; align-items:center; cursor:pointer; }
  .checks input { accent-color:var(--accent); }
  button {
    font:inherit; border-radius:9px; border:1px solid var(--line); background:var(--panel2);
    color:var(--text); padding:8px 14px; cursor:pointer; transition:.15s;
  }
  button:hover:not(:disabled) { border-color:var(--accent); color:var(--text); }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button.primary {
    width:100%; background:var(--accent); border-color:var(--accent);
    color:var(--on-accent);
    font-weight:600; padding:10px; letter-spacing:.05em;
  }
  button.primary:hover:not(:disabled) {
    background:var(--accent-hover); border-color:var(--accent-hover); color:var(--on-accent);
  }
  button.ghost { background:transparent; }
  button.tiny { padding:4px 9px; font-size:12px; border-radius:7px; }
  .hint { color:var(--muted); font-size:11.5px; margin-top:-4px; }
  .authbox {
    display:flex; align-items:center; justify-content:space-between; gap:8px;
    background:var(--panel2); border:1px solid var(--line); border-radius:9px;
    padding:7px 10px; margin-bottom:12px; font-size:12.5px; color:var(--muted);
  }
  .authbox.ok { color:var(--ok); border-color:var(--ok); opacity:.92; }
  .modal { position:fixed; inset:0; background:var(--scrim); display:none; align-items:center; justify-content:center; z-index:120; }
  .modal.show { display:flex; }
  .modal .sheet { background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:22px 26px; text-align:center; box-shadow:var(--shadow); }
  .modal h3 { margin:0 0 6px; font-size:16px; }
  .modal p { color:var(--muted); font-size:12.5px; margin:0 0 14px; line-height:1.7; }
  .modal img { width:240px; height:240px; background:#fff; border-radius:10px; padding:6px; }
  .modal .stat { margin-top:12px; font-size:13px; color:var(--warn); min-height:20px; }
  .proj-list { margin-top:22px; border-top:1px solid var(--line); padding-top:14px; }
  .proj-list h3 { font-size:12px; color:var(--muted); font-weight:600; letter-spacing:.08em; margin:0 0 8px; }
  .proj {
    overflow:hidden;
    padding:7px 9px; border-radius:8px; cursor:pointer; font-size:13px;
    display:flex; justify-content:space-between; gap:8px; color:var(--muted);
  }
  .proj:hover { background:var(--panel2); color:var(--text); }
  .proj.active { background:var(--accent-soft); color:var(--text); border:1px solid var(--accent); }
  .proj small { color:var(--muted); font-size:11px; white-space:nowrap; }
  header.bar { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin-bottom:4px; }
  header.bar h2 { margin:0; font-size:18px; font-weight:600; }
  .sub { color:var(--muted); font-size:12.5px; margin-bottom:16px; }
  .pills { display:flex; gap:7px; flex-wrap:wrap; margin-bottom:16px; }
  .pill { font-size:11.5px; padding:3px 9px; border-radius:999px; border:1px solid var(--line); color:var(--muted); }
  .lic-free { color:var(--ok); border-color:currentColor; }
  .lic-check { color:var(--warn); border-color:currentColor; }
  .lic-unknown { color:var(--bad); border-color:currentColor; }
  .toolbar {
    display:flex; gap:8px; align-items:center; flex-wrap:wrap;
    padding:10px 12px; background:var(--panel); border:1px solid var(--line);
    border-radius:11px; margin-bottom:16px;
  }
  .toolbar .sp { flex:1; }
  .card {
    display:grid; grid-template-columns:26px 1fr; gap:12px;
    background:var(--panel); border:1px solid var(--line); border-radius:13px;
    padding:13px 15px; margin-bottom:11px; transition:.15s;
  }
  .card:hover { border-color:var(--accent); }
  .card.sel { border-color:var(--accent); background:var(--accent-soft); }
  .card input[type=checkbox] { accent-color:var(--accent); width:16px; height:16px; margin-top:4px; cursor:pointer; }
  .card h3 { margin:0 0 5px; font-size:14.5px; font-weight:600; line-height:1.45; }
  .card h3 a { color:var(--text); text-decoration:none; }
  .card h3 a:hover { color:var(--accent); }
  .meta { display:flex; flex-wrap:wrap; gap:12px; font-size:12px; color:var(--muted); align-items:center; }
  .meta b { color:var(--text); font-weight:600; }
  .thumbs { display:flex; gap:6px; margin-top:9px; overflow-x:auto; }
  .thumbs img { width:196px; height:110px; object-fit:cover; border-radius:7px; border:1px solid var(--line); background:#000; cursor:zoom-in; }
  .clipbox { margin-top:9px; font-size:12.5px; display:grid; gap:5px; }
  .clip-row { display:flex; align-items:center; gap:9px; color:var(--muted); }
  .clip-row a { color:var(--accent); text-decoration:none; }
  .clip-row code { background:var(--code-bg); padding:1px 6px; border-radius:5px; font-size:11.5px; }
  .warnline { font-size:12px; color:var(--warn); margin-top:7px; }
  .empty { text-align:center; color:var(--muted); padding:70px 20px; line-height:2; }
  .empty .big { font-size:34px; opacity:.5; }
  #progressbar {
    position:fixed; left:330px; right:0; top:0; height:3px; background:transparent; z-index:60; display:none;
  }
  #progressbar.on { display:block; }
  #progressfill {
    height:100%; width:0%; background:linear-gradient(90deg,var(--accent),var(--accent-hover));
    transition:width .3s ease; box-shadow:0 0 10px var(--accent-soft);
  }
  #console {
    position:fixed; left:330px; right:0; bottom:0; max-height:190px; display:none;
    background:rgba(8,10,15,.96); border-top:1px solid var(--line); overflow-y:auto;
    font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; padding:10px 16px; z-index:50;
  }
  #console .ln { color:var(--muted); white-space:pre-wrap; }
  #console .ln.err { color:var(--bad); }
  #console .prog { color:var(--accent); white-space:pre-wrap; }
  .spinner {
    display:inline-block; width:12px; height:12px; border:2px solid var(--line); border-top-color:var(--accent);
    border-radius:50%; animation:spin .8s linear infinite; vertical-align:-2px; margin-right:6px;
  }
  @keyframes spin { to { transform:rotate(360deg); } }
  .toast {
    position:fixed; top:18px; left:50%; transform:translateX(-50%);
    background:var(--panel); border:1px solid var(--line); color:var(--text);
    padding:9px 18px; border-radius:10px; box-shadow:var(--shadow); z-index:99; font-size:13px;
    opacity:0; transition:opacity .25s; pointer-events:none;
  }
  .toast.show { opacity:1; }
  .lightbox { position:fixed; inset:0; background:var(--scrim); display:none; z-index:100; align-items:center; justify-content:center; cursor:zoom-out; }
  .lightbox img, .lightbox video { max-width:92vw; max-height:92vh; border-radius:10px; }
  .lightbox.show { display:flex; }

  /* ---- 播放器 ---- */
  .player {
    position:sticky; top:-22px; z-index:30; margin:0 0 14px;
    background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:10px;
    box-shadow:var(--shadow);
  }
  .player video {
    width:100%; max-height:min(46vh, 460px); border-radius:8px; background:#000; display:block;
  }
  /* 很寬的螢幕上，播放器靠左、不要拉滿整個寬度 */
  .player .stage { max-width:980px; margin:0 auto; }
  .player .ptitle { font-size:12.5px; color:var(--muted); margin-bottom:7px; display:flex; justify-content:space-between; gap:10px; }
  .player .ptitle b { color:var(--text); font-weight:600; }
  .player .prow { display:flex; gap:8px; align-items:center; margin-top:8px; flex-wrap:wrap; }
  .player .prow a, .player .prow button { font-size:12px; }
  .clip-actions { display:flex; gap:6px; margin-left:auto; }
  .mini {
    font-size:11.5px; padding:3px 9px; border-radius:7px; border:1px solid var(--line);
    background:var(--panel2); color:var(--muted); cursor:pointer;
  }
  .mini:hover { color:var(--text); border-color:var(--accent); }
  .mini.on { border-color:var(--accent); color:var(--text); background:var(--accent-soft); }
  .frames { display:flex; gap:6px; margin-top:8px; overflow-x:auto; }
  .frames img { height:58px; border-radius:6px; cursor:pointer; border:1px solid var(--line); }

  /* ---- 媒體庫 ---- */
  .library { margin-top:18px; border-top:1px solid var(--line); padding-top:14px; }
  .lib-row {
    display:flex; gap:10px; align-items:center; padding:7px 9px; border-radius:9px;
    cursor:pointer; font-size:12.5px; color:var(--muted);
  }
  .lib-row:hover { background:var(--panel2); color:var(--text); }
  .lib-row.playing { background:var(--accent-soft); color:var(--text); }
  .lib-row .dot { width:6px; height:6px; border-radius:50%; background:var(--accent); flex:none; opacity:0; }
  .lib-row.playing .dot { opacity:1; }
  .lib-row .nm { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  @media (max-width: 900px) {
    .app { grid-template-columns:1fr; height:auto; }
    aside { border-right:none; border-bottom:1px solid var(--line); }
    #progressbar {
    position:fixed; left:330px; right:0; top:0; height:3px; background:transparent; z-index:60; display:none;
  }
  #progressbar.on { display:block; }
  #progressfill {
    height:100%; width:0%; background:linear-gradient(90deg,var(--accent),var(--accent-hover));
    transition:width .3s ease; box-shadow:0 0 10px var(--accent-soft);
  }
  #console { left:0; }
    main { padding:18px 14px 120px; }
  }
</style>
</head>
<body>
<div class="app">
  <aside>
    <div class="brandrow">
      <div>
        <h1>vid<span>grab</span></h1>
        <div class="tagline">打關鍵字 → 挑片 → 只切你要的那幾秒</div>
      </div>
      <div class="theme-picker">
        <button class="theme-btn" id="themebtn" title="切換主題" aria-haspopup="true" aria-expanded="false">
          <span id="themeicon">◐</span>
        </button>
        <div class="theme-menu" id="thememenu" role="menu"></div>
      </div>
    </div>
    <div class="buildtag" id="buildtag"></div>

    <label class="field">
      <span>關鍵字（一行一個，或用逗號分隔）</span>
      <textarea id="keywords" placeholder="台北 街景&#10;北歐 空景&#10;寺廟 特寫"></textarea>
    </label>

    <div class="row">
      <label class="field">
        <span>平台</span>
        <select id="platform">
          <option value="both">B 站 + YouTube</option>
          <option value="bilibili">只要 B 站</option>
          <option value="youtube">只要 YouTube</option>
        </select>
      </label>
      <label class="field">
        <span>排序</span>
        <select id="sort">
          <option value="smart">智慧（授權＋熱度）</option>
          <option value="views">觀看最多</option>
          <option value="new">最新</option>
          <option value="license">授權優先</option>
        </select>
      </label>
    </div>

    <div class="row">
      <label class="field">
        <span>每字幾支</span>
        <input type="number" id="per_keyword" value="12" min="1" max="50">
      </label>
      <label class="field">
        <span>最短（分鐘）</span>
        <input type="number" id="min_duration" value="10" min="0" step="1">
      </label>
      <label class="field">
        <span>最長（0=不限）</span>
        <input type="number" id="max_duration" value="0" min="0" step="1">
      </label>
    </div>

    <div class="row">
      <label class="field">
        <span>最近 N 天（0=不限）</span>
        <input type="number" id="fresh_days" value="0" min="0">
      </label>
      <label class="field">
        <span>最少觀看</span>
        <input type="number" id="min_views" value="0" min="0" step="100">
      </label>
    </div>

    <label class="field">
      <span>排除詞（逗號分隔，標題含這些就跳過）</span>
      <input type="text" id="exclude" placeholder="ex. 合集,直播回放">
    </label>

    <div class="checks">
      <label><input type="checkbox" id="only_free"> 只留授權明確的</label>
      <label><input type="checkbox" id="drop_reposts"> 丟掉轉載／非原始出處</label>
      <label><input type="checkbox" id="verify_license"> 逐支確認授權（慢，較準）</label>
    </div>

    <div id="authbox" class="authbox">
      <span id="authwho">檢查 B 站登入…</span>
      <button class="tiny ghost" id="loginbtn" style="white-space:nowrap;flex:none">掃碼登入</button>
    </div>

    <button class="primary" id="go">搜尋素材</button>
    <div class="hint" style="margin-top:8px">長度預設 10 分鐘以上，適合找可反覆取用的長素材。</div>

    <div class="proj-list">
      <h3>我的素材專案</h3>
      <div id="projects"></div>
    </div>
    <div class="resizer" id="resizer" role="separator" aria-orientation="vertical"
         tabindex="0" title="拖曳調整寬度（雙擊還原）"></div>
  </aside>

  <main>
    <div class="player" id="player" style="display:none">
      <div class="ptitle"><b id="playername">—</b><span id="playermeta"></span></div>
      <div class="stage"><video id="videoplayer" controls preload="metadata" playsinline></video></div>
      <div class="prow">
        <button class="mini" id="playerclose">關閉</button>
        <a class="mini" id="playerdownload" href="#" download>下載這個檔案</a>
        <span id="playerframes" class="frames"></span>
      </div>
    </div>
    <div id="view">
      <div class="empty">
        <div class="big">🎬</div>
        先在左邊打關鍵字，按「搜尋素材」。<br>
        搜完可以勾選想要的，只下載你要的那幾秒。
      </div>
    </div>
  </main>
</div>

<div id="progressbar"><div id="progressfill"></div></div>
<div id="console"></div>
<div class="toast" id="toast"></div>
<div class="modal" id="loginmodal">
  <div class="sheet">
    <h3>掃碼登入 B 站</h3>
    <p>手機 B 站 App →「我的」→ 右上角「掃一掃」<br>掃完在手機上按確認</p>
    <img id="qrimg" alt="B 站登入 QR">
    <div class="stat" id="loginstat">等待掃碼…</div>
  </div>
</div>

<div class="lightbox" id="lightbox"></div>

<script>
const $ = (s) => document.querySelector(s);

// ------------------------------------------------------------ 主題與側欄寬度
const THEMES = [
  {id:'midnight', name:'Midnight', kind:'dark',  dot:'#0b0d12', accent:'#6cc0ff'},
  {id:'ink',      name:'Ink',      kind:'dark',  dot:'#08090b', accent:'#c9a227'},
  {id:'forest',   name:'Forest',   kind:'dark',  dot:'#0a1210', accent:'#6fd6a8'},
  {id:'paper',    name:'Paper',    kind:'light', dot:'#f7f4ee', accent:'#b5651d'},
  {id:'mist',     name:'Mist',     kind:'light', dot:'#eef2f7', accent:'#2f6fd0'},
  {id:'sakura',   name:'Sakura',   kind:'light', dot:'#fdf4f5', accent:'#c25a7a'},
];
const THEME_KEY = 'vidgrab.theme';
const SIDEBAR_KEY = 'vidgrab.sidebar';
const SIDEBAR_MIN = 240, SIDEBAR_MAX = 620, SIDEBAR_DEFAULT = 330;

function applyTheme(id) {
  const t = THEMES.find(x => x.id === id) || THEMES[0];
  document.documentElement.dataset.theme = t.id;
  $('#themeicon').textContent = t.kind === 'dark' ? '◐' : '◑';
  // 讓瀏覽器把捲軸、表單控件也跟著換色
  document.documentElement.style.colorScheme = t.kind;
  document.querySelectorAll('#thememenu button').forEach(b => {
    b.setAttribute('aria-checked', String(b.dataset.theme === t.id));
  });
  try { localStorage.setItem(THEME_KEY, t.id); } catch (e) {}
}

function buildThemeMenu() {
  const menu = $('#thememenu');
  const groups = [['dark', '深色'], ['light', '淺色']];
  menu.innerHTML = groups.map(([kind, label]) => `
    <div class="group">${label}</div>
    ${THEMES.filter(t => t.kind === kind).map(t => `
      <button role="menuitemradio" data-theme="${t.id}" aria-checked="false">
        <span class="swatch" style="background:${t.dot};box-shadow:inset 0 0 0 2px ${t.accent}"></span>
        ${t.name}
      </button>`).join('')}
  `).join('');
  menu.querySelectorAll('button').forEach(b => {
    b.onclick = () => { applyTheme(b.dataset.theme); menu.classList.remove('open'); };
  });
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (e) {}
  // 沒存過就跟著系統的深淺
  if (!saved && window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
    saved = 'paper';
  }
  buildThemeMenu();
  applyTheme(saved || 'midnight');

  const btn = $('#themebtn'), menu = $('#thememenu');
  btn.onclick = (e) => {
    e.stopPropagation();
    const open = menu.classList.toggle('open');
    btn.setAttribute('aria-expanded', String(open));
  };
  document.addEventListener('click', () => {
    menu.classList.remove('open');
    btn.setAttribute('aria-expanded', 'false');
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { menu.classList.remove('open'); btn.setAttribute('aria-expanded', 'false'); }
  });
}

function setSidebar(px, persist) {
  const width = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, Math.round(px)));
  document.documentElement.style.setProperty('--sidebar', width + 'px');
  if (persist) { try { localStorage.setItem(SIDEBAR_KEY, String(width)); } catch (e) {} }
  return width;
}

function initResizer() {
  const bar = $('#resizer');
  if (!bar) return;
  let saved = null;
  try { saved = Number(localStorage.getItem(SIDEBAR_KEY)); } catch (e) {}
  // 視窗太窄時不要沿用太寬的側欄
  const maxForViewport = Math.min(SIDEBAR_MAX, Math.round(window.innerWidth * 0.55));
  setSidebar(saved || Math.min(SIDEBAR_DEFAULT, maxForViewport), false);

  let dragging = false;
  const onMove = (e) => {
    if (!dragging) return;
    setSidebar(e.clientX, false);
  };
  const stop = () => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove('dragging');
    document.body.classList.remove('resizing');
    const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar'), 10);
    if (cur) setSidebar(cur, true);
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', stop);
  };
  bar.addEventListener('mousedown', (e) => {
    e.preventDefault();
    dragging = true;
    bar.classList.add('dragging');
    document.body.classList.add('resizing');
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', stop);
  });
  // 雙擊還原
  bar.addEventListener('dblclick', () => setSidebar(SIDEBAR_DEFAULT, true));
  // 鍵盤也能調（無障礙）
  bar.addEventListener('keydown', (e) => {
    const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar'), 10) || SIDEBAR_DEFAULT;
    if (e.key === 'ArrowLeft')  { e.preventDefault(); setSidebar(cur - 16, true); }
    if (e.key === 'ArrowRight') { e.preventDefault(); setSidebar(cur + 16, true); }
    if (e.key === 'Home')       { e.preventDefault(); setSidebar(SIDEBAR_DEFAULT, true); }
  });
  window.addEventListener('resize', () => {
    const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar'), 10) || SIDEBAR_DEFAULT;
    if (window.innerWidth > 820) setSidebar(Math.min(cur, Math.round(window.innerWidth * 0.55)), false);
  });
}

let CURRENT = null;      // 目前專案 payload
let SELECTED = new Set(); // 勾選的 index
let BUSY = false;

// ------------------------------------------------------------ 工具
function toast(msg, ms = 2200) {
  const el = $('#toast'); el.textContent = msg; el.classList.add('show');
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove('show'), ms);
}
let PROG_LINE = null;
function consoleLine(text, isErr) {
  const box = $('#console');
  box.style.display = 'block';
  // 以 \r 開頭 = 覆寫同一行的進度，不要一行行堆
  if (text.startsWith('\r')) {
    if (!PROG_LINE || !PROG_LINE.isConnected) {
      PROG_LINE = document.createElement('div');
      PROG_LINE.className = 'ln prog';
      box.appendChild(PROG_LINE);
    }
    PROG_LINE.textContent = text.slice(1);
    box.scrollTop = box.scrollHeight;
    return;
  }
  PROG_LINE = null;
  const div = document.createElement('div');
  div.className = 'ln' + (isErr ? ' err' : '');
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function tsToSec(s) {
  s = String(s).trim();
  if (s.includes(':')) {
    const p = s.split(':').map(Number);
    return p.length === 3 ? p[0]*3600 + p[1]*60 + p[2] : p[0]*60 + p[1];
  }
  return Number(s);
}
const LIC_CLASS = {free:'lic-free','free-attribution':'lic-free',check:'lic-check',unknown:'lic-unknown',none:'lic-unknown'};

async function api(path, opts) {
  const res = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts));
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

// ------------------------------------------------------------ 專案列表
async function loadProjects() {
  const list = await api('/api/projects');
  const box = $('#projects');
  if (!list.length) { box.innerHTML = '<div class="hint">還沒有專案</div>'; return; }
  box.innerHTML = list.map(p => `
    <div class="proj ${CURRENT && CURRENT.name === p.name ? 'active' : ''}" data-name="${esc(p.name)}">
      <span>${esc((p.queries || []).join('、') || p.name)}</span>
      <small>${p.count}支 / ${p.clips}段</small>
    </div>`).join('');
  box.querySelectorAll('.proj').forEach(el => el.onclick = () => openProject(el.dataset.name));
}

// ------------------------------------------------------------ 開專案
async function openProject(name) {
  try {
    CURRENT = await api('/api/project/' + encodeURIComponent(name));
    SELECTED = new Set();
    render();
    loadProjects();
  } catch (e) { toast('開不起來：' + e.message); }
}

function render() {
  if (!CURRENT) return;
  const s = CURRENT.stats;
  const lic = Object.entries(s.by_license || {}).map(([k, v]) =>
    `<span class="pill ${LIC_CLASS[k] || 'lic-unknown'}">${esc(k)} · ${v}</span>`).join('');
  const items = CURRENT.items || [];

  const cards = items.map(it => {
    const sel = SELECTED.has(it.index);
    const thumbs = it.thumbs.length
      ? `<div class="thumbs">${it.thumbs.map(t => `<img src="${esc(t)}" loading="lazy" onclick="lightbox('img','${esc(t)}')">`).join('')}</div>`
      : '';
    const clips = it.clips.length
      ? `<div class="clipbox">${it.clips.map((c, ci) => `
          <div class="clip-row">
            <a href="${esc(c.rel)}" data-play="${esc(c.rel)}" data-name="${esc(c.name)}">${esc(c.name)}</a>
            <code>${esc(c.span)}</code>
            <span>${esc(c.dims)} · ${esc(c.size)}</span>
            <span class="clip-actions">
              <button class="mini" data-play="${esc(c.rel)}" data-name="${esc(c.name)}">▶ 播放</button>
              <a class="mini" href="${esc(c.rel)}" download>下載</a>
            </span>
          </div>`).join('')}</div>`
      : '';
    const licenseCls = LIC_CLASS[it.license_kind] || 'lic-unknown';
    const notes = [];
    if (it.repost) notes.push('轉載／非原始出處');
    if (it.note) notes.push(it.note);
    if (it.error) notes.push(it.error);
    return `
      <div class="card ${sel ? 'sel' : ''}" data-idx="${it.index}">
        <input type="checkbox" ${sel ? 'checked' : ''} data-idx="${it.index}">
        <div>
          <h3><a href="${esc(it.url)}" target="_blank" rel="noopener">${it.index + 1}. ${esc(it.title)}</a></h3>
          <div class="meta">
            <span>${esc(it.platform_label)}</span>
            <span>${esc(it.author || '—')}</span>
            <span>時長 <b>${esc(it.duration)}</b></span>
            <span>觀看 <b>${esc(it.views_label)}</b></span>
            <span>${esc(it.pubdate || '—')}</span>
            <span class="pill ${licenseCls}">${esc(it.license_short)}</span>
          </div>
          ${thumbs}
          ${notes.length ? `<div class="warnline">⚠ ${esc(notes.join('；'))}</div>` : ''}
          ${clips}
        </div>
      </div>`;
  }).join('');

  $('#view').innerHTML = `
    <header class="bar">
      <h2>${esc((CURRENT.queries || []).join('、'))}</h2>
      <span class="sub" style="margin:0">${items.length} 支候選 · ${s.with_clips} 段已切 · ${esc(CURRENT.clips_bytes)}</span>
    </header>
    <div class="pills">
      <span class="pill">${esc((CURRENT.sources || []).join(' + '))}</span>
      ${lic}
    </div>
    <div class="toolbar">
      <label style="display:flex;gap:6px;align-items:center;font-size:12.5px;color:var(--muted)">
        <input type="checkbox" id="selall"> 全選
      </label>
      <span class="sp"></span>
      <select id="mode" style="width:132px">
        <option value="auto">切片（每段固定）</option>
        <option value="range">切片（自訂區段）</option>
        <option value="full">整支下載</option>
      </select>
      <span class="sub" id="perlabel" style="margin:0">每段</span>
      <input type="number" id="per" value="15" min="1" step="1" style="width:66px">
      <span class="sub" id="persuffix" style="margin:0">秒</span>
      <input type="text" id="starts" placeholder="指定秒數，如 90, 3:20" style="width:170px">
      <input type="text" id="ranges" placeholder="區段，如 1:00-2:30, 5:00-6:00" style="width:230px;display:none">
      <select id="height" style="width:104px">
        <option value="2160">4K</option>
        <option value="1440">1440p</option>
        <option value="1080" selected>1080p</option>
        <option value="720">720p</option>
      </select>
      <label style="display:flex;gap:5px;align-items:center;font-size:12.5px;color:var(--muted)">
        <input type="checkbox" id="frames" checked> 抓畫面
      </label>
      <label style="display:flex;gap:5px;align-items:center;font-size:12.5px;color:var(--muted)">
        <input type="checkbox" id="audio"> 只要聲音
      </label>
      <label style="display:flex;gap:5px;align-items:center;font-size:12.5px;color:var(--muted)"
             title="勾起來：這次用的下載參數會存成預設值">
        <input type="checkbox" id="savesettings"> 記住這些設定
      </label>
      <button class="primary" id="grab" style="width:auto;padding:8px 18px">切片段</button>
      <button class="ghost tiny" id="refresh">重新確認授權</button>
      <button class="ghost tiny" id="openlist">開 HTML 清單</button>
    </div>
    ${items.length ? cards : '<div class="empty">這個專案沒有結果。</div>'}`;

  const cardsEl = document.querySelectorAll('.card input[type=checkbox]');
  cardsEl.forEach(cb => {
    cb.onchange = () => {
      const idx = Number(cb.dataset.idx);
      if (cb.checked) SELECTED.add(idx); else SELECTED.delete(idx);
      cb.closest('.card').classList.toggle('sel', cb.checked);
    };
  });
  $('#selall').onchange = (e) => {
    SELECTED = e.target.checked ? new Set(items.map(i => i.index)) : new Set();
    render();
  };
  const mode = $('#mode');
  const syncMode = () => {
    const m = mode.value;
    $('#ranges').style.display = m === 'range' ? '' : 'none';
    $('#starts').style.display = m === 'auto' ? '' : 'none';
    $('#per').style.display = m === 'full' ? 'none' : '';
    $('#perlabel').style.display = m === 'full' ? 'none' : '';
    $('#persuffix').style.display = m === 'full' ? 'none' : '';
  };
  mode.onchange = syncMode;
  syncMode();
  $('#grab').onclick = grabClips;
  $('#refresh').onclick = refreshProject;
  $('#openlist').onclick = () => api('/api/open', {method:'POST', body: JSON.stringify({project: CURRENT.name})});
}


// ------------------------------------------------------------ 播放已下載的片段
let PLAYING = null;   // 目前播放的檔案路徑

function playClip(rel, name) {
  const box = $('#player'), vid = $('#videoplayer');
  if (PLAYING === rel) {            // 再按一次就收起
    closePlayer();
    return;
  }
  PLAYING = rel;
  box.style.display = '';
  $('#playername').textContent = name || rel.split('/').pop();
  $('#playermeta').textContent = '';
  vid.src = rel;
  vid.play().catch(() => { /* 瀏覽器可能擋自動播放，讓使用者自己按 */ });
  $('#playerdownload').href = rel;

  // 顯示這支影片的幀圖（如果有）
  const frames = $('#playerframes');
  frames.innerHTML = '';
  const item = (CURRENT && CURRENT.items || []).find(it =>
    (it.clips || []).some(c => c.rel === rel));
  const thumbList = (item && item.thumbs) || [];
  if (thumbList.length) {
    frames.innerHTML = thumbList.map(t => `<img src="${esc(t)}" onclick="seekToFrame('${esc(t)}')">`).join('');
  }

  // 標記清單中正在播放的那一列
  document.querySelectorAll('[data-play]').forEach(el => {
    el.classList.toggle('on', el.dataset.play === rel);
  });

  vid.onloadedmetadata = () => {
    const d = vid.duration;
    $('#playermeta').textContent = `${fmtSec(d)} · ${vid.videoWidth}×${vid.videoHeight}`;
  };
  box.scrollIntoView({behavior:'smooth', block:'nearest'});
}

// 點幀圖 → 跳到那個時間點（近似：平均分配在片長裡）
function seekToFrame(src) {
  const vid = $('#videoplayer');
  const item = (CURRENT && CURRENT.items || []).find(it =>
    (it.thumbs || []).some(t => t === src));
  if (!item || !vid.duration) return;
  const idx = item.thumbs.indexOf(src);
  const total = item.thumbs.length || 1;
  // 幀圖是從整支影片抓的，這裡依比例換算
  vid.currentTime = (vid.duration * idx) / Math.max(1, total - 1 || 1);
  vid.pause();
}

function fmtSec(s) {
  if (!isFinite(s)) return '—';
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

function closePlayer() {
  const vid = $('#videoplayer');
  vid.pause(); vid.removeAttribute('src'); vid.load();
  $('#player').style.display = 'none';
  PLAYING = null;
  document.querySelectorAll('[data-play]').forEach(el => el.classList.remove('on'));
}

function initPlayer() {
  $('#playerclose').onclick = closePlayer;
  $('#videoplayer').addEventListener('error', () => {
    $('#playermeta').textContent = '這個格式瀏覽器可能不支援，請按「下載這個檔案」';
  });
  // 事件委派：影片清單是動態重繪的，不能在元素上綁
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-play]');
    if (!el) return;
    e.preventDefault();
    playClip(el.dataset.play, el.dataset.name);
  });
}

function lightbox(kind, src) {
  const box = $('#lightbox');
  box.innerHTML = kind === 'img' ? `<img src="${src}">` : `<video src="${src}" controls autoplay></video>`;
  box.classList.add('show');
  box.onclick = () => { box.classList.remove('show'); box.innerHTML = ''; };
}

// ------------------------------------------------------------ 執行工作
async function follow(jobId, onDone) {
  return new Promise((resolve, reject) => {
    const es = new EventSource('/api/stream/' + jobId);
    es.addEventListener('log', e => {
      const msg = JSON.parse(e.data);
      if (msg.startsWith('\r')) {
        // 進度行：更新頂部細條
        const m = msg.match(/(\d+(?:\.\d+)?)%/);
        const bar = $('#progressbar'), fill = $('#progressfill');
        bar.classList.add('on');
        if (m) fill.style.width = Math.min(100, Number(m[1])) + '%';
      }
      consoleLine(msg);
    });
    es.addEventListener('done', e => {
      es.close(); consoleLine('✔ 完成');
      $('#progressfill').style.width = '100%';
      setTimeout(() => { $('#progressbar').classList.remove('on'); $('#progressfill').style.width = '0%'; }, 900);
      const data = JSON.parse(e.data);
      resolve(data);
    });
    es.addEventListener('error', e => {
      es.close();
      $('#progressbar').classList.remove('on');
      let msg = '發生錯誤';
      try { msg = JSON.parse(e.data); } catch (_) {}
      consoleLine('✗ ' + msg, true);
      reject(new Error(msg));
    });
  });
}

async function search() {
  if (BUSY) return;
  const raw = $('#keywords').value.trim();
  if (!raw) { toast('先輸入關鍵字'); return; }
  const keywords = raw.split(/[\n,，]+/).map(s => s.trim()).filter(Boolean);
  const body = {
    keywords,
    platform: $('#platform').value,
    sort: $('#sort').value,
    per_keyword: Number($('#per_keyword').value) || 12,
    min_duration: Number($('#min_duration').value) || 0,
    max_duration: Number($('#max_duration').value) || 0,
    fresh_days: Number($('#fresh_days').value) || 0,
    min_views: Number($('#min_views').value) || 0,
    exclude: $('#exclude').value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
    only_free: $('#only_free').checked,
    drop_reposts: $('#drop_reposts').checked,
    verify_license: $('#verify_license').checked,
  };
  busy(true, '搜尋中');
  consoleLine('▶ 搜尋：' + keywords.join('、'));
  try {
    const {job} = await api('/api/search', {method:'POST', body: JSON.stringify(body)});
    const res = await follow(job);
    toast(`找到 ${res.count} 支`);
    await loadProjects();
    await openProject(res.project);
  } catch (e) {
    toast(e.message, 4000);
  } finally { busy(false); }
}

async function grabClips() {
  if (BUSY || !CURRENT) return;
  if (!SELECTED.size) { toast('先勾選要的影片'); return; }
  const starts = $('#starts').value.split(/[,，]/).map(s => s.trim()).filter(Boolean)
    .map(tsToSec).filter(n => !isNaN(n));
  const mode = $('#mode').value;
  const rangeSpec = $('#ranges').value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
  const ranges = rangeSpec.map(spec => {
    const m = spec.split(/[-~]/);
    return m.length === 2 ? [tsToSec(m[0]), tsToSec(m[1])] : null;
  }).filter(Boolean);
  if (mode === 'range' && !ranges.length) { toast('請填區段，例如 1:00-2:30'); return; }

  const body = {
    project: CURRENT.name,
    indexes: [...SELECTED],
    per: Number($('#per').value) || 15,
    starts: mode === 'auto' ? starts : [],
    ranges: mode === 'range' ? ranges : [],
    full: mode === 'full',
    height: Number($('#height').value) || 1080,
    frames: $('#frames').checked,
    audio: $('#audio').checked,
    save_settings: $('#savesettings') && $('#savesettings').checked,
  };
  busy(true, '下載中');
  consoleLine(mode === 'full'
    ? `▶ 整支下載 ${body.indexes.length} 支（${body.height}p）`
    : `▶ 下載 ${body.indexes.length} 支，${mode === 'range' ? ranges.length + ' 個區段' : '每段 ' + body.per + ' 秒'}`);
  try {
    const {job} = await api('/api/pick', {method:'POST', body: JSON.stringify(body)});
    const res = await follow(job);
    toast(`完成 ${res.grabbed} 段`);
    await loadProjects();
    await openProject(CURRENT.name);
  } catch (e) {
    toast(e.message, 4000);
  } finally { busy(false); }
}

async function refreshProject() {
  if (BUSY || !CURRENT) return;
  busy(true, '確認中');
  try {
    const {job} = await api('/api/refresh', {method:'POST', body: JSON.stringify({project: CURRENT.name})});
    await follow(job);
    toast('授權已更新');
    await openProject(CURRENT.name);
  } catch (e) { toast(e.message, 4000); } finally { busy(false); }
}

function busy(on, label) {
  BUSY = on;
  const btn = $('#go');
  btn.disabled = on;
  btn.innerHTML = on ? `<span class="spinner"></span>${label}…` : '搜尋素材';
}

// ------------------------------------------------------------ B 站登入
async function checkAuth() {
  try {
    const who = await api('/api/login/status');
    const box = $('#authbox'), label = $('#authwho'), btn = $('#loginbtn');
    if (who.logged_in) {
      box.classList.add('ok');
      label.textContent = 'B 站：' + (who.name || who.mid) + (who.vip ? ' · 大會員' : '');
      btn.textContent = '重新登入';
    } else {
      box.classList.remove('ok');
      label.textContent = 'B 站未登入（高畫質／會員片會失敗）';
      btn.textContent = '掃碼登入';
    }
  } catch (e) { /* 忽略 */ }
}

async function startLogin() {
  const modal = $('#loginmodal');
  $('#loginstat').textContent = '取得 QR…';
  modal.classList.add('show');
  try {
    const res = await api('/api/login/start', {method:'POST', body:'{}'});
    if (res.already) {
      $('#loginstat').textContent = '已經登入了：' + (res.already.name || '');
      setTimeout(() => modal.classList.remove('show'), 1200);
      checkAuth();
      return;
    }
    $('#qrimg').src = res.image + '?t=' + Date.now();
    $('#loginstat').textContent = '等待掃碼…';
    pollLogin(res.key);
  } catch (e) {
    $('#loginstat').textContent = '失敗：' + e.message;
  }
}

async function pollLogin(key) {
  const deadline = Date.now() + 185000;
  const tick = async () => {
    if (Date.now() > deadline) {
      $('#loginstat').textContent = 'QR 過期了，關掉再按一次登入';
      return;
    }
    try {
      const res = await api('/api/login/poll', {method:'POST', body: JSON.stringify({key})});
      $('#loginstat').textContent = res.message;
      if (res.status === 'ok') {
        const who = res.who || {};
        $('#loginstat').textContent = '✔ 登入成功：' + (who.name || '');
        setTimeout(() => $('#loginmodal').classList.remove('show'), 1400);
        checkAuth();
        return;
      }
      if (res.status === 'expired') return;
    } catch (e) { /* 繼續試 */ }
    setTimeout(tick, 2000);
  };
  tick();
}

$('#loginbtn').onclick = startLogin;
$('#loginmodal').onclick = (e) => { if (e.target.id === 'loginmodal') e.currentTarget.classList.remove('show'); };
checkAuth();

$('#go').onclick = search;
$('#keywords').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) search();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { const b = $('#lightbox'); b.classList.remove('show'); b.innerHTML = ''; }
});
async function showBuild() {
  try {
    const v = await api('/api/version');
    $('#buildtag').textContent = 'build ' + v.build;
  } catch (e) { /* 忽略 */ }
}
async function boot() {
  initTheme();
  initResizer();
  initPlayer();
  showBuild();
  await loadProjects();
  try {
    const list = await api('/api/projects');
    if (list.length) openProject(list[0].name);
  } catch (e) { /* 沒有專案就算了 */ }
}
boot();
</script>
</body>
</html>
"""


def main() -> None:  # pragma: no cover
    serve()
