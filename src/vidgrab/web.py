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
import uuid
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
LOCK = threading.Lock()


# ---------------------------------------------------------------- jobs


SUBSCRIBER_QUEUE_SIZE = 500


class Job:
    """一個背景作業。進度事件用 pub/sub 廣播：每個 SSE 連線各自一個有上限的 queue，
    沒有訂閱者（agent 只輪詢 /api/job/{id}）時不累積任何東西。"""

    def __init__(self, kind: str, label: str):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.label = label
        self.messages: list[str] = []
        self.done = False
        self.error = ""
        self.result: dict[str, Any] = {}
        self.started = time.time()
        self.finished = 0.0
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []

    def _publish(self, event: tuple[str, Any]) -> None:
        for q in list(self._subs):
            while True:
                try:
                    q.put_nowait(event)
                    break
                except queue.Full:
                    # 慢的訂閱者：丟最舊的進度，但結尾事件一定要送到
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass

    def emit(self, message: str) -> None:
        with self._lock:
            # 日誌限長：長時間下載會產生上萬行，全留在記憶體裡沒有意義
            if len(self.messages) < _job_limits()[2]:
                self.messages.append(message)
            self._publish(("log", message))

    def finish(self, result: dict[str, Any]) -> None:
        with self._lock:
            self.done = True
            self.finished = time.time()
            self.result = result
            self._publish(("done", result))

    def fail(self, message: str) -> None:
        with self._lock:
            self.done = True
            self.finished = time.time()
            self.error = message
            self._publish(("error", message))

    def subscribe(self) -> tuple[list[str], queue.Queue | None, tuple[str, Any] | None]:
        """回傳 (歷史日誌, 訂閱用 queue, 已結束時的結尾事件)。歷史與訂閱在同一把鎖內取得，不漏不重。"""
        with self._lock:
            history = list(self.messages)
            if self.done:
                final = ("error", self.error) if self.error else ("done", self.result)
                return history, None, final
            q: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
            self._subs.append(q)
            return history, q, None

    def unsubscribe(self, q: queue.Queue | None) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

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


# ---------------------------------------------------------------- 認證

TOKEN_ENV = "VIDGRAB_API_TOKEN"
TOKEN_COOKIE = "vidgrab_token"
# 這些路徑不需要 token：頁面外殼、靜態檔、版本（Docker healthcheck 用）、API 文件
PUBLIC_ROUTES = ("/", "/index.html", "/api/version", "/api/openapi.json", "/docs", "/docs/")


def api_token() -> str:
    """設定的 API token；空字串＝不啟用認證（只適合綁本機時）。只讀環境變數，
    不放 config.json，因為 /api/config 會把設定回給呼叫端。"""
    import os

    return (os.environ.get(TOKEN_ENV) or "").strip()


def _token_matches(candidate: str, token: str) -> bool:
    import hmac

    return bool(candidate) and hmac.compare_digest(candidate.encode(), token.encode())


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


# HTTP 狀態 ↔ 錯誤碼（與 CLI 的 code 同一套）
STATUS_CODES = {400: "bad_input", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                405: "method_not_allowed",
                409: "conflict", 413: "too_large", 429: "rate_limited", 500: "internal",
                503: "unavailable", 504: "timeout"}
ERROR_STATUS = {"bad_input": 400, "no_targets": 400, "ambiguous_project": 409, "unauthorized": 401,
                "forbidden": 403, "not_found": 404, "no_project": 404, "login_required": 403,
                "missing_ffmpeg": 503, "download_failed": 502, "timeout": 504, "internal": 500}


def error_body(message: str, *, status: int = 400, code: str | None = None, hint: str = "") -> dict[str, Any]:
    err: dict[str, Any] = {
        "code": code or STATUS_CODES.get(status, "error"),
        "message": message,
        "retryable": status >= 500 or status == 429,
    }
    if hint:
        err["hint"] = hint
    return {"ok": False, "error": err}


def project_dir(name: str) -> Path | None:
    """專案名稱 → 專案資料夾；名稱不合法或不在素材庫根目錄內就回 None。"""
    name = urllib.parse.unquote(name)
    # 專案名稱只能是單層資料夾名：拒絕 / \\ .. 與空字串
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        return None
    root = tasks.projects_root().resolve()
    path = (root / name).resolve()
    # 用 relative_to 比目錄層級；str.startswith 會讓 projects-old 通過 projects 的檢查
    if not _is_within(path, root) or path == root or not (path / "manifest.json").exists():
        return None
    return path


def _is_within(path: Path, root: Path) -> bool:
    """path 是否在 root 底下（以目錄層級判斷，兩者都先 resolve）。"""
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """解析單一 Range。回傳 (start, end)、None（沒有 Range 或無法解析，當整檔）、
    或 "unsatisfiable"（要回 416）。支援 `a-b`、`a-` 與 suffix `-N`（最後 N bytes）。"""
    if not header or not header.startswith("bytes="):
        return None
    spec = header.split("=", 1)[1].split(",")[0].strip()
    lo, sep, hi = spec.partition("-")
    if not sep:
        return None
    try:
        if not lo:
            n = int(hi)
            if n <= 0:
                return "unsatisfiable"
            if size == 0:
                return "unsatisfiable"
            return max(0, size - n), size - 1
        start = int(lo)
        end = int(hi) if hi else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or (hi and end < start):
        return "unsatisfiable"
    return start, min(end, size - 1)


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


# 這三個是「預設值」，實際以 config 為準（見 _job_limits()）。
MAX_FINISHED_JOBS = 40
JOB_TTL = 30 * 60          # 完成後保留 30 分鐘，足夠你回頭看結果
MAX_JOB_MESSAGES = 2000    # 單一 job 的日誌上限，避免長時間下載把記憶體吃光


def _job_limits() -> tuple[int, float, int]:
    """(保留筆數, TTL 秒, 單一作業日誌上限)，以 config 為準。"""
    from . import config

    def pick(key, fallback, cast):
        try:
            return cast(config.get(key, fallback))
        except Exception:  # noqa: BLE001
            return fallback

    return (
        max(1, pick("max_finished_jobs", MAX_FINISHED_JOBS, int)),
        max(60.0, pick("job_ttl_minutes", int(JOB_TTL // 60), int) * 60.0),
        max(100, pick("max_job_messages", MAX_JOB_MESSAGES, int)),
    )


def _prune_jobs() -> None:
    """清掉已經結束的舊 job。

    這裡原本只寫不刪：每跑一次搜尋／下載就留一個 Job（含完整結果與日誌），
    開著一整天會一直長大。
    """
    now = time.time()
    keep, ttl, _ = _job_limits()
    with LOCK:
        dead = [
            jid for jid, job in JOBS.items()
            if getattr(job, "done", False)
            and now - getattr(job, "finished", getattr(job, "started", now)) > ttl
        ]
        for jid in dead:
            JOBS.pop(jid, None)

        # 就算都還很新，也不要無限累積
        finished = sorted(
            ((getattr(j, "finished", getattr(j, "started", 0)), jid)
             for jid, j in JOBS.items() if getattr(j, "done", False)),
            reverse=True,
        )
        for _, jid in finished[keep:]:
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


def wait_job(job: Job, limit_seconds: float) -> bool:
    """等 job 做完，最多 limit_seconds 秒；回傳是否已完成。"""
    until = time.time() + limit_seconds
    while not job.done and time.time() < until:
        time.sleep(0.2)
    return job.done


def make_search_job(body: dict[str, Any]) -> Job:
    """啟動搜尋 job（HTTP /api/search 與 MCP search_footage 共用）。壞輸入丟 BadInput。"""
    from . import config as cfg
    from .models import BadInput

    queries = body.get("keywords") or []
    if isinstance(queries, str):
        queries = [queries]
    queries = [str(q) for q in queries if str(q).strip()]
    if not queries:
        raise BadInput("請至少輸入一個關鍵字", hint='keywords 是字串陣列，例如 ["台北 街景"]')

    saved = cfg.load()

    def pick(key, cast=None):
        if body.get(key) is not None:
            return body[key]
        value = saved.get(key, cfg.DEFAULTS.get(key))
        return cast(value) if cast and value is not None else value

    try:
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
            "orientation": str(body.get("orientation") or "any"),
        }
    except (TypeError, ValueError) as exc:
        raise BadInput(f"參數格式不對：{exc}") from None
    if body.get("save_settings"):
        cfg.save_patch({k: opts[k] for k in opts if k in cfg.DEFAULTS})

    def work(emit):
        res = tasks.run_search(queries, emit=emit, **opts)
        return {"project": res["project"].name, "count": len(res["items"])}

    return start_job("search", "、".join(queries), work)


def make_batch_job(body: dict[str, Any]) -> tuple[Job, float | None]:
    """驗證參數並啟動批次 job（HTTP /api/batch 與 MCP grab_clip 共用）。回 (job, timeout)。"""
    from . import agent
    from . import config as cfg
    from .models import BadInput, NotFound, VidgrabError

    project = project_dir(str(body.get("project", "")))
    if not project:
        raise NotFound(f"找不到專案：{body.get('project', '')}", hint="用 vault／list_assets 看有哪些專案")
    _, items = tasks.load_project(project)
    targets = agent.resolve_targets(
        items, indexes=body.get("indexes"), asset_ids=body.get("asset_ids"),
        top=body.get("top"), all_items=bool(body.get("all")),
    )
    if not targets:
        raise VidgrabError("沒有指定要抓的項目", code="no_targets",
                           hint="給 indexes、asset_ids、top 或 all 其中一個")
    timeout = body.get("timeout")
    try:
        timeout = float(timeout) if timeout not in (None, "", 0) else None
    except (TypeError, ValueError):
        raise BadInput("timeout 必須是秒數") from None
    try:
        opts = resolve_pick_options(body, cfg.load())
    except (TypeError, ValueError) as exc:
        raise BadInput(f"參數格式不對：{exc}",
                       hint="ranges 例如 [[60,75]] 或 [\"1:00-1:15\"]") from None
    opts["dry_run"] = bool(body.get("dry_run"))

    def work(emit):
        return agent.run_batch(project, targets, timeout=timeout, emit=emit, **opts)

    return start_job("batch", f"{project.name} · batch {len(targets)} 支", work), timeout


# ---------------------------------------------------------------- 資料


def _fmt_ts(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _to_file_url(path_like: str | None, project: Path) -> str:
    """把專案裡的檔案變成 /file/... 網址；不在專案內就回空字串。

    兩邊都 resolve()：macOS 的 /var 是 /private/var 的軟連結，
    素材放在 /tmp、/var 底下時字串前綴會對不上（實際踩過）。
    """
    if not path_like:
        return ""
    try:
        p = Path(path_like).resolve()
        base = project.resolve()
    except OSError:
        return ""
    if not p.is_file():
        return ""
    try:
        rel = p.relative_to(base)
    except ValueError:
        return ""
    return (f"/file/{urllib.parse.quote(project.name)}/"
            f"{urllib.parse.quote(str(rel))}")


def _thumb_url(thumb: str | None, project: Path) -> str:
    """把縮圖路徑變成 /file/... 這種可以給 <img> 用的網址。

    兩邊都要 resolve()：macOS 的 /var 是 /private/var 的軟連結，
    素材放在 /tmp、/var 底下時，路徑字串一個有 /private、一個沒有，
    relative_to 就會 ValueError，縮圖永遠是空的（實際踩過）。
    """
    return _to_file_url(thumb, project)


def clip_info(clip: dict[str, Any], project: Path, root: Path,
              item_index: int | None = None) -> dict[str, Any]:
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
        # 指回是哪一支候選：沒有這個，拿到切片的人只能靠檔名前綴猜
        "item_index": item_index,
        # 檔案可能被手動刪掉、被同步工具搬走、或硬碟拔了。
        # 仍然列出來（讓使用者知道「這裡本來有東西」），
        # 但標記出來，前端才不會給一個點下去 404 的連結。
        "missing": not path.exists(),
    }


def item_payload(item: Item, index: int, project: Path) -> dict[str, Any]:
    thumbs = [u for u in (_to_file_url(f, project) for f in item.thumbs) if u]
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
        "clips": [clip_info(c, project, project, index) for c in item.clips],
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




def _ranges(raw: list) -> list[tuple[float, float]]:
    """ranges 可以是 [[60, 75], ...]，也可以是 ["1:00-1:15", ...]（跟 CLI --range 同寫法）。"""
    from .cli import _parse_ranges

    out: list[tuple[float, float]] = []
    for pair in raw:
        if isinstance(pair, str):
            out.extend(_parse_ranges([pair]))
        else:
            out.append(tuple(float(x) for x in pair))  # type: ignore[arg-type]
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
        "ranges": _ranges(body.get("ranges") or []),
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


# 回應體的結構。沒有這些，外部工具只知道「會回 200」，
# 不知道拿回來的是什麼 —— Postman／程式碼產生器到這裡就沒戲了。
COMPONENTS = {
    "schemas": {
        "Error": {
            "type": "object",
            "description": "出錯時的回應（HTTP 4xx／5xx），與 CLI 同一套 code",
            "properties": {
                "ok": {"type": "boolean", "enum": [False]},
                "error": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "bad_input / not_found / unauthorized / timeout / internal …"},
                        "message": {"type": "string"},
                        "retryable": {"type": "boolean", "description": "重試有沒有機會成功"},
                        "hint": {"type": "string", "description": "下一步該怎麼做"},
                    },
                    "required": ["code", "message", "retryable"],
                },
            },
            "required": ["error"],
        },
        "AgentClip": {
            "type": "object",
            "description": "給 agent 的片段資訊（穩定 id、絕對路徑與 HTTP 網址）",
            "properties": {
                "id": {"type": "string", "example": "bilibili:BV1xx#60-75"},
                "asset_id": {"type": "string"},
                "path": {"type": "string", "description": "伺服器上的絕對路徑"},
                "url": {"type": "string", "description": "/file/... 取檔網址（跨機器用這個）"},
                "thumb_url": {"type": "string"},
                "exists": {"type": "boolean"},
                "start": {"type": "number", "nullable": True},
                "end": {"type": "number", "nullable": True},
                "duration": {"type": "number", "nullable": True},
                "width": {"type": "integer", "nullable": True},
                "height": {"type": "integer", "nullable": True},
                "orientation": {"type": "string", "enum": ["portrait", "landscape", ""]},
                "cached": {"type": "boolean"},
                "license": {"type": "object"},
                "credit_hint": {"type": "string"},
            },
        },
        "Vault": {
            "type": "object",
            "description": "vidgrab.vault/1：整個素材庫（或單一專案、或 query 命中）",
            "properties": {
                "schema": {"type": "string", "example": "vidgrab.vault/1"},
                "root": {"type": "string"},
                "totals": {"type": "object"},
                "projects": {"type": "array", "items": {"type": "object"}},
                "query": {"type": "string"},
                "matches": {"type": "array", "items": {"type": "object"}},
            },
        },
        "FindResult": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "item": {"type": "object"},
                "clip": {"$ref": "#/components/schemas/AgentClip"},
            },
        },
        "BatchResult": {
            "type": "object",
            "description": "vidgrab.batch/1；非同步呼叫時回 {job, status_url}，結果在 job.result",
            "properties": {
                "schema": {"type": "string", "example": "vidgrab.batch/1"},
                "project": {"type": "string"},
                "requested": {"type": "integer"},
                "ok": {"type": "integer"},
                "failed": {"type": "integer"},
                "timed_out": {"type": "boolean"},
                "elapsed_seconds": {"type": "number"},
                "dry_run": {"type": "boolean"},
                "results": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "asset_id": {"type": "string"},
                        "title": {"type": "string"},
                        "new_clips": {"type": "array", "items": {"$ref": "#/components/schemas/AgentClip"}},
                        "total_clips": {"type": "integer"},
                        "error": {"type": "string", "nullable": True},
                    },
                }},
                "error": {"type": "object", "description": "逾時時的結構化錯誤"},
                "job": {"type": "string"},
                "status_url": {"type": "string"},
            },
        },
        "Version": {
            "type": "object",
            "properties": {
                "version": {"type": "string", "example": "0.2.0"},
                "build": {"type": "string", "example": "33.df8dd79"},
                "full": {"type": "string", "example": "0.2.0+33.df8dd79"},
            },
        },
        "ProjectSummary": {
            "type": "object",
            "description": "素材庫清單裡的一個專案（輕量）",
            "properties": {
                "name": {"type": "string"},
                "title": {"type": "string"},
                "pinned": {"type": "boolean"},
                "note": {"type": "string"},
                "missing": {"type": "boolean", "description": "資料夾不見了"},
                "queries": {"type": "array", "items": {"type": "string"}},
                "count": {"type": "integer", "description": "候選數"},
                "clips": {"type": "integer", "description": "已切片數"},
                "created": {"type": "string"},
                "size": {"type": "string", "example": "60.9MB"},
            },
        },
        "Clip": {
            "type": "object",
            "description": "一段切好的影片",
            "properties": {
                "name": {"type": "string"},
                "rel": {"type": "string", "description": "/file/... 網址"},
                "url": {"type": "string"},
                "start": {"type": "number"},
                "end": {"type": "number"},
                "full": {"type": "boolean", "description": "整支而非切片"},
                "span": {"type": "string", "example": "21:37→21:45"},
                "size": {"type": "string", "example": "7.1MB"},
                "dims": {"type": "string", "example": "1920×1080"},
                "orientation": {"type": "string", "enum": ["landscape", "portrait", ""]},
                "thumb": {"type": "string", "description": "縮圖網址，沒有就是空字串"},
                "item_index": {"type": ["integer", "null"],
                               "description": "這一段來自哪一支候選（0 起算）"},
                "missing": {"type": "boolean",
                            "description": "檔案不在磁碟上（被刪或搬走），不要給連結"},
            },
        },
        "Item": {
            "type": "object",
            "description": "一支候選影片",
            "properties": {
                "index": {"type": "integer", "description": "0 起算"},
                "vid": {"type": "string"},
                "platform": {"type": "string", "enum": ["bilibili", "youtube"]},
                "platform_label": {"type": "string"},
                "title": {"type": "string"},
                "author": {"type": "string"},
                "url": {"type": "string"},
                "keyword": {"type": "string"},
                "duration": {"type": "string",
                             "description": "給人看的長度，例如 3:00:11"},
                "duration_sec": {"type": "number", "description": "實際秒數"},
                "views": {"type": ["integer", "null"]},
                "views_label": {"type": "string"},
                "pubdate": {"type": "string"},
                "license_kind": {"type": "string"},
                "license_label": {"type": "string"},
                "license_short": {"type": "string"},
                "repost": {"type": "boolean"},
                "error": {"type": "string"},
                "note": {"type": "string"},
                "thumbs": {"type": "array", "items": {"type": "string"}},
                "clips": {"type": "array", "items": {"$ref": "#/components/schemas/Clip"}},
            },
        },
        "Project": {
            "type": "object",
            "description": "單一專案的完整內容",
            "properties": {
                "name": {"type": "string"},
                "path": {"type": "string"},
                "created": {"type": "string"},
                "updated": {"type": "string"},
                "queries": {"type": "array", "items": {"type": "string"}},
                "sources": {"type": "array", "items": {"type": "string"}},
                "clips_bytes": {"type": "string",
                                "description": "給人看的總大小，例如 60.9MB"},
                "stats": {
                    "type": "object",
                    "properties": {
                        "total": {"type": "integer"},
                        "by_platform": {"type": "object", "additionalProperties": {"type": "integer"}},
                        "by_license": {"type": "object", "additionalProperties": {"type": "integer"}},
                        "reposts": {"type": "integer"},
                        "with_clips": {"type": "integer"},
                        "clip_bytes": {"type": "integer"},
                    },
                },
                "items": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}},
            },
        },
        "JobRef": {
            "type": "object",
            "description": "非同步工作；用 id 去輪詢或訂閱",
            "properties": {"job": {"type": "string"}},
            "required": ["job"],
        },
        "Job": {
            "type": "object",
            "description": "工作狀態；還沒跑完時 result 不存在",
            "properties": {
                "id": {"type": "string"},
                "done": {"type": "boolean"},
                "error": {"type": "string"},
                "messages": {"type": "array", "items": {"type": "string"}},
                "result": {"type": "object"},
            },
        },
        "Config": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "config.json 的位置"},
                "values": {"type": "object", "additionalProperties": True},
                "defaults": {"type": "object", "additionalProperties": True,
                             "description": "出廠預設值，對照 values 看哪些被改過"},
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "group": {"type": "string"},
                            "label": {"type": "string"},
                            "type": {"type": "string"},
                            "value": {},
                            "default": {},
                            "help": {"type": "string"},
                            "changed": {"type": "boolean"},
                            "min": {"type": "number"},
                            "max": {"type": "number"},
                            "options": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
        "Export": {
            "type": "object",
            "description": "備份快照：拿去別的機器還原用",
            "properties": {
                "version": {"type": "string"},
                "config": {"type": "object", "additionalProperties": True},
                "logged_in": {"type": "boolean"},
                "projects": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/ProjectSummary"},
                },
            },
        },
        "LibraryScan": {
            "type": "object",
            "description": "掃描 projects/ 目錄的結果",
            "properties": {
                "root": {"type": "string"},
                "ok": {"type": "boolean"},
                "tracked": {"type": "integer", "description": "已納入素材庫的數量"},
                "on_disk": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "folder": {"type": "string"},
                            "queries": {"type": "array", "items": {"type": "string"}},
                            "created": {"type": "string"},
                        },
                    },
                },
                "untracked": {"type": "array", "items": {"type": "string"},
                              "description": "在磁碟上但沒納入的資料夾"},
                "missing": {"type": "array", "items": {"type": "string"},
                            "description": "納入了但資料夾不見了"},
            },
        },
        "Library": {
            "type": "object",
            "properties": {
                "projects": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/ProjectSummary"},
                },
                "order": {"type": "array", "items": {"type": "string"}},
                "sort": {"type": "string"},
            },
        },
        "LoginStatus": {
            "type": "object",
            "properties": {
                "logged_in": {"type": "boolean"},
                "name": {"type": "string", "description": "沒登入時不存在"},
                "mid": {"type": "integer", "description": "沒登入時不存在"},
                "vip": {"type": "boolean"},
            },
            "required": ["logged_in"],
        },
    }
}

def _docs_html() -> str:
    """把 OpenAPI 規格變成一頁可讀的文件。

    刻意不載 swagger-ui：那要多帶幾百 KB 的 CDN 資源，
    而這個工具本身就是「不要依賴外部服務」的取向。
    """
    import html as _html

    rows: list[str] = []
    for path, ops in OPENAPI["paths"].items():
        for method, op in ops.items():
            if method not in ("get", "post", "put", "delete"):
                continue
            tags = ", ".join(op.get("tags") or [])
            # 請求參數：路徑參數 + requestBody 的 properties
            params: list[str] = []
            for prm in op.get("parameters") or []:
                need = "" if prm.get("required") else "?"
                params.append(f'{prm.get("name", "?")}{need}'
                              f' <span class="ty">{_html.escape(str(prm.get("in", "")))}</span>')
            body = (op.get("requestBody") or {}).get("content", {}).get(
                "application/json", {}).get("schema", {})
            required = set(body.get("required") or [])
            for key, spec in (body.get("properties") or {}).items():
                star = "" if key in required else "?"
                desc = _html.escape(str(spec.get("description", "")))
                params.append(f'{_html.escape(key)}{star}'
                              f' <span class="ty">{_html.escape(str(spec.get("type", "")))}</span>'
                              + (f' <span class="d">{desc}</span>' if desc else ""))

            resp = op.get("responses", {}).get("200", {})
            rcontent = resp.get("content", {})
            if "application/json" in rcontent:
                sch = rcontent["application/json"].get("schema", {})
                ref = sch.get("$ref", "")
                if ref:
                    rtype = ref.rsplit("/", 1)[-1]
                elif sch.get("type") == "array":
                    rtype = f'{sch.get("items", {}).get("$ref", "").rsplit("/", 1)[-1]}[]'
                else:
                    rtype = sch.get("type", "object")
                resp_txt = _html.escape(rtype or "object")
            elif rcontent:
                resp_txt = _html.escape(next(iter(rcontent)))
            else:
                resp_txt = "—"

            rows.append(f"""<details class="ep">
  <summary><span class="m {method}">{method.upper()}</span>
    <span class="path">{_html.escape(path)}</span>
    <span class="sum">{_html.escape(op.get("summary", ""))}</span></summary>
  <div class="body">
    <div class="line"><b>標籤</b> {_html.escape(tags)}</div>
    <div class="line"><b>回傳</b> <code>{resp_txt}</code></div>
    {('<div class="line"><b>參數</b></div><ul class="prm">' + "".join(
        f"<li>{x}</li>" for x in params) + "</ul>") if params else ""}
  </div>
</details>""")

    schemas = "".join(
        f'<div class="schema"><b>{_html.escape(name)}</b>'
        f'<span class="d">{_html.escape(str(spec.get("description", "")))}</span>'
        + ("<ul>" + "".join(
            f'<li>{_html.escape(k)}'
            f' <span class="ty">{_html.escape(str(v.get("type", v.get("$ref", "")).__class__.__name__ if isinstance(v.get("type"), dict) else v.get("type", "ref")))}</span>'
            f'</li>' for k, v in (spec.get("properties") or {}).items()) + "</ul>"
           if spec.get("properties") else "")
        + "</div>"
        for name, spec in COMPONENTS["schemas"].items()
    )

    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>vidgrab API</title>
<style>
  :root {{ color-scheme: dark light; }}
  body {{ margin:0; padding:28px 32px 80px; font:14px/1.6 -apple-system,"PingFang TC",
         "Noto Sans TC",system-ui,sans-serif; background:#0e1014; color:#e8ecf2;
         max-width:1000px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .meta {{ color:#8b94a3; font-size:12.5px; margin-bottom:20px; }}
  .meta code {{ background:#1a1e26; padding:2px 6px; border-radius:5px; }}
  h2 {{ font-size:15px; margin:26px 0 10px; color:#c9d2e0; }}
  .ep {{ background:#161a21; border:1px solid #232833; border-radius:9px;
         margin-bottom:7px; overflow:hidden; }}
  .ep summary {{ padding:9px 13px; cursor:pointer; display:flex; gap:10px;
                 align-items:center; flex-wrap:wrap; }}
  .ep summary::marker {{ color:#5f6b7d; }}
  .m {{ font:600 10.5px/1 ui-monospace,monospace; padding:4px 7px; border-radius:4px;
        letter-spacing:.05em; flex:none; }}
  .m.get {{ background:#1d3a5c; color:#8ec5ff; }}
  .m.post {{ background:#1e4030; color:#84e0b0; }}
  .m.put, .m.delete {{ background:#4a2530; color:#f2a0b0; }}
  .path {{ font:12.5px/1 ui-monospace,monospace; color:#e8ecf2; }}
  .sum {{ color:#8b94a3; font-size:12.5px; }}
  .body {{ padding:4px 13px 12px 13px; border-top:1px solid #232833; }}
  .line {{ margin:7px 0; font-size:12.5px; }}
  .line b {{ color:#8b94a3; font-weight:500; margin-right:7px; }}
  .ty {{ font:11px/1 ui-monospace,monospace; color:#7f8b9d; background:#1e232c;
         padding:2px 5px; border-radius:4px; }}
  .d {{ color:#6f7a8b; font-size:12px; }}
  ul {{ margin:5px 0 0; padding-left:20px; }}
  li {{ font:12px/1.8 ui-monospace,monospace; color:#c2cbd8; }}
  .schemas {{ display:grid; gap:8px; }}
  .schema {{ background:#161a21; border:1px solid #232833; border-radius:9px;
             padding:10px 13px; }}
  .schema b {{ font:600 12.5px/1 ui-monospace,monospace; color:#8ec5ff; }}
  .schema .d {{ display:block; margin-top:3px; }}
  a {{ color:#8ec5ff; }}
  @media (prefers-color-scheme: light) {{
    body {{ background:#f7f9fc; color:#1a2029; }}
    .meta, .sum, .line b, .d {{ color:#5c6675; }}
    .ep, .schema {{ background:#fff; border-color:#dde3ec; }}
    .path {{ color:#1a2029; }}
    .ty {{ background:#eef2f7; color:#5c6675; }}
    li {{ color:#2c3542; }}
    .m.get {{ background:#dbeafe; color:#1d4ed8; }}
    .m.post {{ background:#d7f5e3; color:#157347; }}
    .m.put, .m.delete {{ background:#fde2e6; color:#a3182f; }}
    .ep summary::marker {{ color:#9aa5b5; }}
  }}
</style></head><body>
<h1>vidgrab API</h1>
<div class="meta">
  OpenAPI {OPENAPI["openapi"]} · {sum(1 for ops in OPENAPI["paths"].values() for m in ops if m in ("get","post","put","delete"))} 個端點、{len(OPENAPI["paths"])} 條路徑 ·
  機器可讀版本：<code><a href="/api/openapi.json">/api/openapi.json</a></code><br>
  長時間的工作是非同步的：先 POST 拿 job id，再輪詢
  <code>/api/job/{{id}}</code> 或訂閱 <code>/api/stream/{{id}}</code>。
</div>
<h2>端點</h2>
{"".join(rows)}
<h2>資料結構</h2>
<div class="schemas">{schemas}</div>
</body></html>"""


OPENAPI: dict[str, Any] = {
    "openapi": "3.0.3",
    "components": COMPONENTS,
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
        {"name": "agent", "description": "給 AI agent：穩定 id、一次拿完整狀態、冪等批次"},
    ],
    "paths": {
        "/api/version": {
            "get": {"tags": ["meta"], "summary": "版本與 build number",
                    "responses": {"200": {"description": "版本資訊", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Version"}}}}}},
        },
        "/api/openapi.json": {
            "get": {"tags": ["meta"], "summary": "這份 OpenAPI 規格",
                    "responses": {"200": {"description": "OpenAPI 3.0 文件", "content": {"application/json": {"schema": {"type": "object"}}}}}},
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
                "responses": {"200": {"description": "原始檔案（影片／縮圖）", "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}},
                              "404": {"description": "找不到", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}},
            },
        },
        "/api/config": {
            "get": {"tags": ["meta"], "summary": "讀設定（含預設值）",
                    "responses": {"200": {"description": "目前的設定", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Config"}}}}}},
            "post": {
                "tags": ["meta"], "summary": "改設定，存過就成為預設",
                "requestBody": {
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "example": {"set": {"per": 30, "height": 720, "platform": "bilibili"}},
                    }}}
                },
                "responses": {"200": {"description": "更新後的設定", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Config"}}}}, "400": {"description": "值不合法", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}},
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
                            "platform": {"type": "string", "enum": ["both", "bilibili", "youtube", "pexels", "pixabay", "stock"]},
                            "per_keyword": {"type": "integer"},
                            "orientation": {"type": "string", "enum": ["any", "portrait", "landscape", "square"],
                                            "description": "只對素材站（pexels／pixabay）有效"},
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
                "responses": {"200": {"description": "已排入搜尋工作", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/JobRef"}}}}},
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
                "responses": {"200": {"description": "完整備份（版本、設定、登入狀態、專案索引）", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Export"}}}}},
            },
        },
        "/api/vault": {
            "get": {"tags": ["agent"], "summary": "素材庫快照（agent 主要入口）",
                    "parameters": [
                        {"name": "project", "in": "query", "schema": {"type": "string"}, "description": "只看單一專案"},
                        {"name": "query", "in": "query", "schema": {"type": "string"}, "description": "在已下載的素材裡搜尋"},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}},
                        {"name": "items", "in": "query", "schema": {"type": "string", "default": "1"}, "description": "0 = 不要逐支明細"},
                        {"name": "description", "in": "query", "schema": {"type": "string"}, "description": "1 = 附完整簡介"},
                    ],
                    "responses": {"200": {"description": "vidgrab.vault/1", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Vault"}}}},
                                  "404": {"description": "找不到專案", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}}},
        },
        "/api/find/{id}": {
            "get": {"tags": ["agent"], "summary": "用 asset id 或片段 id 找回素材（/api/clip/{id} 同義）",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"},
                                    "description": "bilibili:BV1xx 或 bilibili:BV1xx#60-75（# 要編成 %23）"}],
                    "responses": {"200": {"description": "命中的素材（與片段）", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/FindResult"}}}}}},
        },
        "/api/batch": {
            "post": {
                "tags": ["agent"], "summary": "批次抓片（冪等、部分成功、可設期限）",
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "required": ["project"],
                    "properties": {
                        "project": {"type": "string"},
                        "indexes": {"type": "array", "items": {"type": "integer"}},
                        "asset_ids": {"type": "array", "items": {"type": "string"}},
                        "top": {"type": "integer"},
                        "all": {"type": "boolean"},
                        "ranges": {"type": "array", "items": {}, "description": "[[60,75]] 或 [\"1:00-1:15\"]"},
                        "starts": {"type": "array", "items": {"type": "number"}},
                        "per": {"type": "number"},
                        "full": {"type": "boolean"},
                        "height": {"type": "integer"},
                        "audio": {"type": "boolean"},
                        "audio_format": {"type": "string"},
                        "frames": {"type": "boolean"},
                        "frame_count": {"type": "integer"},
                        "dry_run": {"type": "boolean"},
                        "timeout": {"type": "number", "description": "批次最多跑幾秒"},
                        "wait": {"type": "boolean", "description": "true = 這個請求等到做完才回"},
                    },
                }}}},
                "responses": {"200": {"description": "wait=true 時是 vidgrab.batch/1，否則 {job, status_url}", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/BatchResult"}}}}},
            },
        },
        "/api/job/{id}": {
            "get": {"tags": ["job"], "summary": "查工作狀態與完整訊息",
                    "parameters": [{"name": "id", "in": "path", "required": True,
                                    "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "工作狀態", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Job"}}}}}},
        },
        "/api/stream/{id}": {
            "get": {"tags": ["job"], "summary": "SSE 即時進度（log / done / error）",
                    "parameters": [{"name": "id", "in": "path", "required": True,
                                    "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "SSE 串流：log / progress / done / error 四種事件", "content": {"text/event-stream": {"schema": {"type": "string"}}}}}},
        },
        "/api/projects": {
            "get": {"tags": ["project"], "summary": "列出所有專案",
                    "responses": {"200": {"description": "專案清單", "content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/ProjectSummary"}}}}}}},
        },
        "/api/project/{name}": {
            "get": {"tags": ["project"], "summary": "單一專案完整內容（含候選與已下載片段）",
                    "parameters": [{"name": "name", "in": "path", "required": True,
                                    "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "專案內容", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Project"}}}}}},
        },
        "/api/item/{name}/{index}": {
            "get": {"tags": ["project"], "summary": "單一候選影片",
                    "parameters": [
                        {"name": "name", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "index", "in": "path", "required": True, "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"description": "單一候選", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}}}},
        },
        "/api/library/scan": {
            "get": {"tags": ["project"], "summary": "掃描 projects/，找出未納入與遺失的專案",
                    "responses": {"200": {"description": "掃描結果", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/LibraryScan"}}}}}},
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
                    "responses": {"200": {"description": "素材庫", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Library"}}}}}},
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
                "responses": {"200": {"description": "更新後的素材庫", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Library"}}}},
                              "400": {"description": "參數不合法", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}},
            },
        },
        "/api/project/delete": {
            "post": {"tags": ["project"], "summary": "刪除整個專案（連同已下載檔案）",
                     "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ProjectSummary"}}}}}},
        },
        "/api/delete-clip": {
            "post": {"tags": ["project"], "summary": "刪除單一片段檔",
                     "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}}}},
        },
        "/api/refresh": {
            "post": {"tags": ["project"], "summary": "重新確認授權與影片是否還在（非同步）",
                     "responses": {"200": {"description": "回傳 {job: id}", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/JobRef"}}}}}},
        },
        "/api/export": {
            "get": {"tags": ["project"], "summary": "匯出全部設定與專案索引（備份用）",
                    "responses": {"200": {"description": "完整備份（版本、設定、登入狀態、專案索引）", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Export"}}}}}},
        },
        "/api/login/start": {
            "post": {"tags": ["auth"], "summary": "產生 B 站登入 QR",
                     "responses": {"200": {"description": "回傳 {key, image}", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/JobRef"}}}}}},
        },
        "/api/login/qr": {
            "get": {"tags": ["auth"], "summary": "QR 圖（PNG）",
                    "responses": {"200": {"description": "QR 圖（PNG）", "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}}},
        },
        "/api/login/poll": {
            "post": {"tags": ["auth"], "summary": "查掃碼結果",
                     "responses": {"200": {"description": "status: waiting/scanned/ok/expired", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/LoginStatus"}}}}}},
        },
        "/api/login/status": {
            "get": {"tags": ["auth"], "summary": "目前登入的帳號",
                    "responses": {"200": {"description": "登入結果", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/LoginStatus"}}}}}},
        },
        "/api/login/logout": {
            "post": {"tags": ["auth"], "summary": "登出並刪除 cookie",
                     "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/LoginStatus"}}}}}},
        },
        "/api/open": {
            "post": {"tags": ["project"], "summary": "用瀏覽器打開 HTML 清單",
                     "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ProjectSummary"}}}}}},
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
        if code >= 400 and isinstance(data, dict) and isinstance(data.get("error"), str):
            # 錯誤一律是 {ok:false, error:{code,message,retryable,hint}}，跟 CLI 同一套
            data = error_body(data["error"], status=code, code=data.get("code"), hint=data.get("hint", ""))
        self._send(code, json.dumps(_jsonable(data), ensure_ascii=False).encode())

    def _fail(self, exc: Exception) -> None:
        """VidgrabError → 對應 HTTP 狀態碼與結構化錯誤；其他例外 → 500 internal。"""
        from .models import VidgrabError

        if isinstance(exc, VidgrabError):
            status = ERROR_STATUS.get(exc.code, 400)
            self._send(status, json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False).encode())
            return
        traceback.print_exc()
        body = error_body(f"{type(exc).__name__}: {exc}", status=500)
        self._send(500, json.dumps(body, ensure_ascii=False).encode())

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _project_dir(self, name: str) -> Path | None:
        return project_dir(name)

    # -- 認證 ---------------------------------------------------------

    def _authorized(self, route: str) -> bool:
        """沒設 token → 一律放行；有設 → 公開路徑以外都要 Bearer header 或登入後的 cookie。"""
        token = api_token()
        if not token or route in PUBLIC_ROUTES or route.startswith("/static/"):
            return True
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer ") and _token_matches(header[7:].strip(), token):
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == TOKEN_COOKIE and _token_matches(urllib.parse.unquote(value), token):
                return True
        return False

    def _deny(self) -> None:
        body = json.dumps(error_body(
            "需要 API token",
            status=401,
            hint="帶 Authorization: Bearer <token>，瀏覽器則先開 /?token=<token> 登入",
        ), ensure_ascii=False).encode()
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Bearer realm="vidgrab"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _login_redirect(self, query: str) -> bool:
        """/?token=xxx：token 正確就種 HttpOnly cookie 並導回 /，讓網頁介面（img/video 沒辦法帶 header）可用。"""
        token = api_token()
        if not token:
            return False
        given = (urllib.parse.parse_qs(query).get("token") or [""])[0]
        if not _token_matches(given, token):
            return False
        self.send_response(302)
        self.send_header("Set-Cookie",
                         f"{TOKEN_COOKIE}={urllib.parse.quote(token)}; Path=/; HttpOnly; SameSite=Strict")
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    # -- 路由 ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html") and parsed.query and self._login_redirect(parsed.query):
            return
        if not self._authorized(route):
            self._deny()
            return

        if route in ("/", "/index.html"):
            self._send(200, render_page().encode(), "text/html; charset=utf-8")
            return
        if route == "/api/projects":
            self._json(projects_payload())
            return
        if route == "/mcp":
            self._json({"error": "MCP 端點只接受 POST（JSON-RPC）"}, 405)
            return
        if route == "/api/vault":
            from . import agent

            q = urllib.parse.parse_qs(parsed.query)
            first = lambda k, d="": (q.get(k) or [d])[0]  # noqa: E731
            try:
                limit = max(1, min(200, int(first("limit", "20"))))
            except ValueError:
                limit = 20
            try:
                self._json(agent.vault_query(
                    project=first("project"), query=first("query"), limit=limit,
                    with_items=first("items", "1") not in ("0", "false"),
                    with_description=first("description") in ("1", "true"),
                ))
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)
            return
        if route.startswith("/api/find/") or route.startswith("/api/clip/"):
            from . import agent

            needle = urllib.parse.unquote(route.split("/", 3)[3])
            hit = agent.find_by_id(needle) if needle else None
            if hit is None:
                self._json({"error": f"找不到：{needle}",
                            "hint": "id 長得像 bilibili:BV1xx 或 bilibili:BV1xx#60-75"}, 404)
                return
            self._json(hit)
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
        if route in ("/docs", "/docs/"):
            # 可瀏覽的 API 文件：不需要另開伺服器，也不需要外掛。
            # 不引入 swagger-ui（要多帶一整包 CDN 資源），自己畫一份：
            # 端點、參數、回應結構、可展開的範例。
            self._send(200, _docs_html().encode("utf-8"),
                       "text/html; charset=utf-8")
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
        if route.startswith("/api/"):
            # /api/ 開頭一律回 JSON：agent 與程式碼用戶端都只解析 JSON，
            # 這裡吐純文字會讓它在 json.loads 就炸掉（實際踩過）。
            self._json({"error": f"沒有這個端點：{route}"}, 404)
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

        if not self._authorized(route):
            self._deny()
            return
        if not self._origin_ok():
            self._json({"error": "跨站請求已被拒絕"}, 403)
            return

        if route == "/mcp":
            self._mcp()
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
            try:
                job = make_search_job(body)
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)
                return
            self._json({"job": job.id})
            return

        if route == "/api/batch":
            self._batch(body)
            return

        if route == "/api/pick":
            project = self._project_dir(body.get("project", ""))
            if not project:
                self._json({"error": "找不到專案"}, 404)
                return
            try:
                indexes = [int(i) for i in (body.get("indexes") or [])]
            except (TypeError, ValueError):
                self._json({"error": "indexes 必須是整數陣列"}, 400)
                return
            if not indexes:
                # 少了這個 return 就會「送完 400 再起一個 job」，
                # 連線已被標記回應完成，第二次寫入直接讓請求爛掉。
                self._json({"error": "請先勾選影片"}, 400)
                return
            # 索引要先對得上候選，否則工作會在背景丟 IndexError，
            # 使用者只看到「已完成」卻什麼都沒下載（實際踩過）。
            _, items = tasks.load_project(project)
            bad = [i for i in indexes if i < 0 or i >= len(items)]
            if bad:
                self._json({"error": f"編號超出範圍（0–{len(items) - 1}）：{bad}"}, 400)
                return
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
                from . import agent

                before = agent.clip_keys(items, indexes)
                res = tasks.run_pick(project, indexes, emit=emit, **opts)
                results = agent.new_clips(project, indexes, before)
                return {"project": project.name, "grabbed": res["grabbed"], "count": len(indexes),
                        "results": results,
                        "new_clips": [c for r in results for c in r["new_clips"]]}

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
                from .sources import source_for

                sources: dict[str, Any] = {}
                for item in items:
                    if item.platform not in sources:
                        sources[item.platform] = source_for(item.platform)
                    src = sources[item.platform]
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
                root = tasks.projects_root().resolve()
                target = Path(body.get("path", "")).resolve()
                if not _is_within(target, root) or not target.is_file():
                    raise ValueError("不合法路徑")
                rel = target.relative_to(root).parts
                if len(rel) < 2:
                    raise ValueError("不合法路徑")
                project = root / rel[0]
                if not (project / "manifest.json").exists():
                    raise ValueError("不合法路徑")
                manifest, items = tasks.load_project(project)
                # 只能刪 manifest 登記過的片段檔，且只移除「路徑相同」的那筆記錄
                def same(c):
                    cp = c.get("path", "")
                    if not cp:
                        return False
                    cpath = Path(cp)
                    if not cpath.is_absolute():
                        cpath = project / cpath
                    return cpath.resolve() == target

                if not any(same(c) for item in items for c in item.clips):
                    raise ValueError("這個檔案不是已登記的片段")
                target.unlink()
                for item in items:
                    item.clips = [c for c in item.clips if not same(c)]
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

    def _mcp(self) -> None:
        """POST /mcp：MCP streamable HTTP（JSON-RPC，回 application/json）。"""
        from . import mcp

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            reply: Any = mcp._rpc_error(None, -32700, "Parse error")
        else:
            reply = mcp.handle_payload(payload)
        if reply is None:  # 只有通知：202、沒有內容
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(200, json.dumps(reply, ensure_ascii=False).encode())

    def _batch(self, body: dict[str, Any]) -> None:
        """POST /api/batch：agent 的批次入口，跟 CLI `batch` 同一份實作。

        預設非同步（回 job，結果是 vidgrab.batch/1）；`wait: true` 時在這個請求裡等到做完
        （最多 `timeout` 秒，到期會中止下載並回部分結果）。
        """
        try:
            job, timeout = make_batch_job(body)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
            return
        if not body.get("wait"):
            self._json({"job": job.id, "status_url": f"/api/job/{job.id}"})
            return
        # 同步等待：比批次期限多留一點時間收尾；沒設期限就最多等 30 分鐘
        if not wait_job(job, timeout + 30 if timeout else 1800):
            self._json({"job": job.id, "done": False, "status_url": f"/api/job/{job.id}"}, 202)
            return
        if job.error:
            self._json({"error": job.error, "job": job.id}, 500)
            return
        self._json(job.result)

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
        history, q, final = job.subscribe()
        try:
            for message in history:
                self._log_event(message)
            if final is not None:
                self._event(*final)
                return
            assert q is not None
            while True:
                try:
                    kind, payload = q.get(timeout=5)
                except queue.Empty:
                    # 客戶端可能早就斷了。先用 select 確認連線還在，斷了就立刻收工。
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
        finally:
            job.unsubscribe(q)

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
        root = tasks.projects_root().resolve()
        project = root / project_name
        target = (project / rel).resolve()
        # 不能用 str.startswith 判斷！那是路徑穿越漏洞：
        #   /projects/victim  vs  /projects/victim-evil/secret.txt
        #   字串前綴成立，於是 `victim` 這個專案可以讀到 `victim-evil` 的檔案。
        # 用 relative_to 才是真的在比目錄層級。
        try:
            base = project.resolve()
            base.relative_to(root)
            if base == root:
                raise ValueError("project root")
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
        rng = parse_range(self.headers.get("Range"), size)
        if rng == "unsatisfiable":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = 0, size - 1
        if rng:
            start, end = rng
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(max(0, end - start + 1)))
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
