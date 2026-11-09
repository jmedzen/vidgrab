"""MCP server：讓 Claude Code 等 MCP client 直接把 vidgrab 當素材庫用。

兩種傳輸，共用同一份工具實作：

- Streamable HTTP：`vidgrab web` 的 `POST /mcp`（JSON-RPC，回 application/json；
  認證與其他 `/api/*` 一樣，設了 `VIDGRAB_API_TOKEN` 就要 Bearer）
- stdio：`vidgrab mcp`（給本機 client 以子行程啟動）

刻意不引入 `mcp` 套件：協定的這個子集（initialize／tools）幾十行就能實作，
而且不用讓 Docker 映像多裝一堆依賴。

原則：工具只回 **JSON 與網址，不回二進位內容**。片段用 `url`（`/file/...`，要加上 vidgrab 的位址
取檔）或 `path`（同機器時）取得。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from . import __version__
from .models import VidgrabError

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

# 搜尋、下載會跑很久；工具預設等這麼久，沒做完就回 job id，之後用 get_job 查
DEFAULT_WAIT = 90.0


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_footage",
        "description": (
            "去 B 站／YouTube／Pexels／Pixabay 搜新的影片素材（Pexels、Pixabay 授權明確可商用，platform 用 stock 兩個都搜，需 API key，英文關鍵字效果最好），建立一個專案並回傳候選清單（含時長、授權、縮圖、原始頁面）。"
            "要找『手上已有的』素材請用 list_assets。搜完用 grab_clip 下載想要的片段。"
        ),
        "inputSchema": _obj({
            "keywords": {"type": "array", "items": {"type": "string"},
                         "description": "關鍵字，每個分開搜再合併（B 站建議也試簡體）"},
            "platform": {"type": "string", "enum": ["both", "bilibili", "youtube", "pexels", "pixabay", "stock", "openverse", "jamendo", "music"]},
            "orientation": {"type": "string", "enum": ["any", "portrait", "landscape", "square"],
                            "description": "畫面方向，只對素材站有效；做 Shorts 用 portrait"},
            "per_keyword": {"type": "integer", "description": "每個關鍵字抓幾支"},
            "min_duration": {"type": "number", "description": "最短幾分鐘（出廠 10，找長素材用）"},
            "max_duration": {"type": "number", "description": "最長幾分鐘，0 = 不限"},
            "only_free": {"type": "boolean", "description": "只留授權明確的"},
            "exclude": {"type": "array", "items": {"type": "string"}, "description": "標題含這些詞就跳過"},
            "name": {"type": "string", "description": "專案名稱，不給就自動命名"},
            "timeout": {"type": "number", "description": f"最多等幾秒（預設 {int(DEFAULT_WAIT)}），沒做完回 job id"},
        }, ["keywords"]),
    },
    {
        "name": "list_assets",
        "description": (
            "查素材庫（已下載的東西）。不給參數 = 各專案概況；給 project = 該專案每支素材與片段；"
            "給 query = 在已下載的素材裡搜尋標題／作者／標籤。先用這個看手上有什麼，再決定要不要搜新的。"
        ),
        "inputSchema": _obj({
            "project": {"type": "string"},
            "query": {"type": "string", "description": "空白分隔多個詞"},
            "limit": {"type": "integer", "description": "query 命中最多幾筆（預設 20）"},
        }),
    },
    {
        "name": "find_asset",
        "description": "用 asset id（bilibili:BV1xx）或片段 id（bilibili:BV1xx#60-75）找回素材與它的片段、檔案網址、授權。",
        "inputSchema": _obj({"id": {"type": "string"}}, ["id"]),
    },
    {
        "name": "grab_clip",
        "description": (
            "從專案的候選裡下載整支或切片。冪等：同一個範圍已經抓過就不重抓，new_clips 為空。"
            "選目標用 asset_ids／indexes／top／all 其中一個；選範圍用 ranges（[[60,75]] 或 [\"1:00-1:15\"]）、"
            "starts＋per、或 full。回傳每個新片段的 id、path、url、duration、orientation、license、credit_hint。"
        ),
        "inputSchema": _obj({
            "project": {"type": "string"},
            "asset_ids": {"type": "array", "items": {"type": "string"}},
            "indexes": {"type": "array", "items": {"type": "integer"}},
            "top": {"type": "integer"},
            "all": {"type": "boolean"},
            "ranges": {"type": "array", "items": {}, "description": "[[開始秒, 結束秒], ...] 或 [\"1:00-1:15\"]"},
            "starts": {"type": "array", "items": {"type": "number"}, "description": "開始時間（秒），每段長度為 per"},
            "per": {"type": "number", "description": "starts／自動挑點時每段幾秒"},
            "full": {"type": "boolean", "description": "整支下載"},
            "height": {"type": "integer", "description": "最高解析度，例如 1080"},
            "audio": {"type": "boolean", "description": "只要聲音"},
            "frames": {"type": "boolean", "description": "順便抓畫面"},
            "dry_run": {"type": "boolean", "description": "只回報要抓什麼，不動網路"},
            "timeout": {"type": "number", "description": f"批次期限（秒），到期中止並回部分結果；預設最多等 {int(DEFAULT_WAIT)} 秒"},
        }, ["project"]),
    },
    {
        "name": "grab_frame",
        "description": (
            "單格畫面（封面、縮圖、分鏡），回圖檔 url／path。id 給片段 id（bilibili:BV1xx#60-75）就從已下載的片段抽，"
            "很快；給 asset id 則一定要 at，沒下載過那一段會只抓那一秒去抽。at 是原影片的秒數。"
        ),
        "inputSchema": _obj({
            "id": {"type": "string", "description": "片段 id 或 asset id"},
            "at": {"type": "number", "description": "原影片的秒數；片段 id 可省略（取正中間）"},
            "width": {"type": "integer", "description": "預設 640"},
            "format": {"type": "string", "enum": ["jpg", "png"]},
            "project": {"type": "string"},
        }, ["id"]),
    },
    {
        "name": "get_job",
        "description": "查背景工作（search_footage／grab_clip 沒在時限內做完時回的 job id）。done 為 true 時 result 就是完成結果。",
        "inputSchema": _obj({"job_id": {"type": "string"}}, ["job_id"]),
    },
]


# ---------------------------------------------------------------- 工具實作


def _job_outcome(job, wait: float) -> dict[str, Any]:
    from .web import wait_job

    if not wait_job(job, wait):
        return {"done": False, "job": job.id,
                "hint": "還在跑；稍後用 get_job 查，或到期的批次會自己中止並回部分結果"}
    if job.error:
        raise VidgrabError(job.error, code="internal", retryable=True)
    return {"done": True, "job": job.id, "result": job.result}


def _tool_search_footage(args: dict[str, Any]) -> Any:
    from . import agent, tasks
    from .web import make_search_job

    wait = float(args.get("timeout") or DEFAULT_WAIT)
    out = _job_outcome(make_search_job(args), wait)
    if out["done"]:
        project = tasks.projects_root() / out["result"]["project"]
        summary = agent.project_summary(project, with_items=True)
        out["project"] = summary["name"]
        out["candidates"] = [
            {"index": n, **{k: it[k] for k in (
                "id", "platform", "title", "author", "duration", "width", "height",
                "license", "credit_hint", "webpage_url")}}
            for n, it in enumerate(summary.get("items", []))
        ]
        out["next"] = "用 grab_clip 下載：project + asset_ids（或 indexes）+ ranges／full"
    return out


def _tool_list_assets(args: dict[str, Any]) -> Any:
    from . import agent

    project = str(args.get("project") or "")
    query = str(args.get("query") or "")
    return agent.vault_query(project=project, query=query, limit=int(args.get("limit") or 20),
                             with_items=bool(project or query))


def _tool_find_asset(args: dict[str, Any]) -> Any:
    from . import agent
    from .models import NotFound

    needle = str(args.get("id") or "")
    hit = agent.find_by_id(needle) if needle else None
    if hit is None:
        raise NotFound(f"找不到：{needle}", hint="id 長得像 bilibili:BV1xx 或 bilibili:BV1xx#60-75")
    return hit


def _tool_grab_clip(args: dict[str, Any]) -> Any:
    from .web import make_batch_job

    body = dict(args)
    if not body.get("timeout"):
        body["timeout"] = DEFAULT_WAIT
    job, timeout = make_batch_job(body)
    # 批次到期會自己中止並回部分結果；多留一點時間讓它收尾
    out = _job_outcome(job, (timeout or DEFAULT_WAIT) + 30)
    return out["result"] if out["done"] else out


def _tool_grab_frame(args: dict[str, Any]) -> Any:
    from . import agent

    return agent.grab_frame(str(args.get("id") or ""), args.get("at"), width=args.get("width") or 640,
                            fmt=args.get("format") or "jpg", project=str(args.get("project") or ""))


def _tool_get_job(args: dict[str, Any]) -> Any:
    from .models import NotFound
    from .web import JOBS

    job = JOBS.get(str(args.get("job_id") or ""))
    if job is None:
        raise NotFound("找不到這個 job（可能已過期）", hint="已完成的 job 只保留一陣子，結果請用 list_assets 重查")
    return job.snapshot()


HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "search_footage": _tool_search_footage,
    "list_assets": _tool_list_assets,
    "find_asset": _tool_find_asset,
    "grab_clip": _tool_grab_clip,
    "grab_frame": _tool_grab_frame,
    "get_job": _tool_get_job,
}


def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """執行工具，回 MCP 的 CallToolResult。錯誤用 isError＋結構化內容，不丟例外給 client。"""
    from .web import _jsonable

    handler = HANDLERS.get(name)
    if handler is None:
        return _result({"ok": False, "error": {"code": "not_found", "message": f"沒有這個工具：{name}",
                                               "retryable": False}}, error=True)
    try:
        return _result(_jsonable(handler(arguments or {})))
    except VidgrabError as exc:
        return _result({"ok": False, "error": exc.to_dict()}, error=True)
    except SystemExit as exc:
        return _result({"ok": False, "error": {"code": "bad_input", "message": str(exc), "retryable": False}},
                       error=True)
    except Exception as exc:  # noqa: BLE001
        return _result({"ok": False, "error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}",
                                               "retryable": True}}, error=True)


def _result(data: Any, *, error: bool = False) -> dict[str, Any]:
    res: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
        "isError": error,
    }
    if isinstance(data, dict):
        res["structuredContent"] = data
    return res


# ---------------------------------------------------------------- JSON-RPC


def _rpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(msg: Any) -> dict[str, Any] | None:
    """處理一則 JSON-RPC 訊息。通知（沒有 id）回 None。"""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "Invalid Request")
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "vidgrab", "version": __version__},
            "instructions": (
                "素材庫：search_footage 找新片 → grab_clip 下載整支或切片 → list_assets／find_asset 查已有的。"
                "回傳的 license／credit_hint 要看：授權『未知』預設不能直接公開使用。"
            ),
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        if not isinstance(params.get("name"), str):
            return _rpc_error(msg_id, -32602, "tools/call 需要 name")
        args = params.get("arguments")
        if args is not None and not isinstance(args, dict):
            return _rpc_error(msg_id, -32602, "arguments 必須是物件")
        return {"jsonrpc": "2.0", "id": msg_id, "result": call_tool(params["name"], args)}
    if is_notification:
        return None  # notifications/initialized、notifications/cancelled …
    return _rpc_error(msg_id, -32601, f"Method not found: {method}")


def handle_payload(payload: Any) -> Any | None:
    """HTTP 用：單一訊息或 batch（陣列）。全是通知就回 None（HTTP 202）。"""
    if isinstance(payload, list):
        if not payload:
            return _rpc_error(None, -32600, "Invalid Request")
        replies = [r for r in (handle_message(m) for m in payload) if r is not None]
        return replies or None
    return handle_message(payload)


def serve_stdio() -> int:
    """stdio 傳輸：一行一則 JSON-RPC。stdout 只能有協定訊息，其餘（log）全走 stderr。"""
    real_out = sys.stdout
    sys.stdout = sys.stderr
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle_payload(json.loads(line))
        except json.JSONDecodeError:
            reply = _rpc_error(None, -32700, "Parse error")
        if reply is not None:
            real_out.write(json.dumps(reply, ensure_ascii=False) + "\n")
            real_out.flush()
    return 0
