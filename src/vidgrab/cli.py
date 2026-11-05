"""命令列介面：search / pick / refresh / show / list / open / web。"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import webbrowser
from pathlib import Path

# 必須在 requests/urllib3 被 import 之前過濾，否則它會先印出警告
def _quiet_ssl_noise() -> None:
    """某些 Python（如 Xcode 內建的 3.9、Homebrew 的 LibreSSL）連不到 OpenSSL，
    urllib3 會在 import 時就碎念一次。

    這跟工具本身無關，但會把 CLI 輸出弄得很吵。
    這裡刻意「不 import urllib3」來取類別——那樣做本身就會先觸發警告，
    所以改用訊息比對，在它被印出來之前就攔掉。
    """
    import warnings

    warnings.filterwarnings("ignore", message=r".*only supports OpenSSL.*")
    warnings.filterwarnings("ignore", message=r".*NotOpenSSLWarning.*")


_quiet_ssl_noise()

from . import media, report, tasks  # noqa: E402  （要排在警告過濾之後）
from .version import __version__, build, full_version  # noqa: E402
from .models import (  # noqa: E402
    Item,
    fmt_duration,
    human_size,
    log,
    pad,
    read_json,
    slugify,
    today,
)

PLATFORM_LABEL = {"bilibili": "B站", "youtube": "YT"}
SHORT_LICENSE = {
    "free": "可自由使用",
    "free-attribution": "可用需標註",
    "check": "需確認",
    "unknown": "未知",
    "none": "無資訊",
}


def resolve_project(value: str | None) -> Path:
    if not value:
        cands = sorted([p for p in tasks.projects_root().glob("*") if (p / "manifest.json").exists()])
        if not cands:
            raise SystemExit("這底下還沒有專案，先跑一次 search。")
        log(f"（沒指定專案，用最新的：{cands[-1].name}）")
        return cands[-1]
    path = Path(value).expanduser()
    if path.is_absolute():
        if (path / "manifest.json").exists():
            return path
        raise SystemExit(f"找不到專案（沒有 manifest.json）：{path}")
    if (Path.cwd() / path / "manifest.json").exists():
        return Path.cwd() / path
    candidate = tasks.projects_root() / value
    if (candidate / "manifest.json").exists():
        return candidate
    matches = [p for p in tasks.projects_root().glob(f"*{value}*") if (p / "manifest.json").exists()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SystemExit("對到多個專案：\n  " + "\n  ".join(m.name for m in matches))
    raise SystemExit(f"找不到專案：{value}")


# ---------------------------------------------------------------- 顯示


def print_table(items: list[Item]) -> None:
    if not items:
        print("（沒有結果）")
        return
    header = f"{'#':>3}  {pad('源', 4)}{pad('長度', 10)}{pad('觀看', 9)}{pad('授權', 16)}{pad('標題', 56)}作者"
    print(header)
    print("─" * min(len(header) + 10, 130))
    for idx, item in enumerate(items, 1):
        lic = SHORT_LICENSE.get(item.license_kind, "未知")
        flag = "／轉載" if item.repost else ""
        print(
            f"{idx:>3}  {pad(PLATFORM_LABEL.get(item.platform, item.platform), 4)}"
            f"{pad(fmt_duration(item.duration), 10)}"
            f"{pad(_views(item.views), 9)}"
            f"{pad(lic + flag, 16)}"
            f"{pad(item.title, 56)}{item.author}"
        )


def _views(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value / 10000:.1f}萬" if value >= 10000 else str(value)


def print_summary(items: list[Item], project: Path) -> None:
    stats = report.summarize(items)
    print()
    print(f"共 {stats['total']} 支：B 站 {stats['by_platform']['bilibili']}、YouTube {stats['by_platform']['youtube']}")
    if stats["by_license"]:
        parts = [f"{SHORT_LICENSE.get(k, k)} {v}" for k, v in sorted(stats["by_license"].items())]
        print("授權：" + "、".join(parts))
    if stats["reposts"]:
        print(f"其中 {stats['reposts']} 支標記為轉載／非原始出處")
    if stats["with_clips"]:
        print(f"已切片段 {stats['with_clips']} 段，{human_size(stats['clip_bytes'])}")
    print(f"清單：{project / 'index.html'}")


def parse_selector(text: str, total: int) -> list[int]:
    picks: list[int] = []
    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            lo, hi = chunk.split("-", 1)
            picks.extend(range(int(lo), int(hi) + 1))
        else:
            picks.append(int(chunk))
    return [p - 1 for p in picks if 1 <= p <= total]


# ---------------------------------------------------------------- 指令


SEARCH_KEYS = (
    "platform", "per_keyword", "order", "duration", "min_duration", "max_duration",
    "fresh_days", "min_views", "only_free", "drop_reposts", "verify_license", "sort",
)


def effective(args: argparse.Namespace, keys: tuple[str, ...]) -> dict:
    """使用者有給就用給的，沒給就吃設定檔；另外把明確給的記下來給 --save 用。"""
    from . import config

    saved = config.load()
    values = {}
    for key in keys:
        attr = key.replace("-", "_")
        given = getattr(args, attr, None)
        values[key] = saved.get(key) if given is None else given
    return values


def cmd_search(args: argparse.Namespace) -> int:
    from . import config

    values = effective(args, SEARCH_KEYS)
    exclude = args.exclude if args.exclude else config.load().get("exclude")

    if args.save:
        patch = {k: v for k, v in values.items() if getattr(args, k, None) is not None}
        if args.exclude:
            patch["exclude"] = args.exclude
        config.save_patch(patch)
        print(f"✔ 已存成預設：{', '.join(sorted(patch))}（{config.config_path()}）")

    result = tasks.run_search(
        args.keywords,
        platform=values["platform"],
        per_keyword=values["per_keyword"],
        order=values["order"],
        duration=values["duration"],
        exclude=exclude,
        fresh_days=values["fresh_days"],
        min_duration=values["min_duration"],
        max_duration=values["max_duration"],
        min_views=values["min_views"],
        only_free=values["only_free"],
        drop_reposts=values["drop_reposts"],
        verify_license=values["verify_license"],
        sort=values["sort"],
        name=args.name,
        out=args.out,
    )
    project, items = result["project"], result["items"]
    if args.json:
        print(json.dumps({"project": str(project), "items": [i.to_dict() for i in items]},
                         ensure_ascii=False, indent=2))
        return 0
    print_table(items)
    print_summary(items, project)
    print()
    print("下一步：")
    print(f"  ./vidgrab pick {project.name} --top 3 --per 8 --frames")
    print(f"  ./vidgrab pick {project.name} --select 1,4 --at 1:30 --per 12")
    print(f"  ./vidgrab web          # 開 Web UI")
    return 0


def _parse_ranges(specs: list[str]) -> list[tuple[float, float]]:
    """把 "1:00-2:30" 這種寫法轉成 (start, end) 秒數。"""
    from .models import parse_ts as _ts

    out: list[tuple[float, float]] = []
    for spec in specs:
        for chunk in spec.replace(" ", "").split(","):
            if not chunk:
                continue
            if "-" not in chunk:
                raise SystemExit(f"區段要寫成 開始-結束，例如 1:00-2:30（收到 {chunk}）")
            lo, _, hi = chunk.partition("-")
            start, end = _ts(lo), _ts(hi)
            if end <= start:
                raise SystemExit(f"區段結束要比開始晚：{chunk}")
            out.append((start, end))
    return out


def cmd_pick(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    if args.select:
        manifest, items = tasks.load_project(project)
        idxs = parse_selector(args.select, len(items))
    elif args.top:
        manifest, items = tasks.load_project(project)
        idxs = list(range(min(args.top, len(items))))
    else:
        raise SystemExit("要指定 --select 1,3-5 或 --top 3。")
    if not idxs:
        raise SystemExit("選到的編號都是空的。")

    from . import config
    from .models import parse_ts

    values = effective(args, ("per", "height", "audio", "audio_format", "frames", "frame_count"))
    if args.save:
        patch = {k: v for k, v in values.items() if getattr(args, k, None) is not None}
        config.save_patch(patch)
        print(f"✔ 已存成預設：{', '.join(sorted(patch))}（{config.config_path()}）")

    result = tasks.run_pick(
        project,
        idxs,
        per=float(values["per"]),
        starts=[parse_ts(a) for a in (args.at or [])],
        ranges=_parse_ranges(args.range or []),
        full=bool(args.full),
        frames=values["frames"],
        frame_count=int(values["frame_count"]),
        height=int(values["height"]),
        audio=values["audio"],
        audio_format=values["audio_format"],
        cookies=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
        fmt=args.format,
        dry_run=args.dry_run,
        frame_only=args.frame_only,
    )
    if args.dry_run:
        print("\n（--dry-run：沒有下載任何東西）")
    else:
        print_summary(result["items"], project)
        if args.open:
            webbrowser.open(f"file://{project / 'index.html'}")
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    manifest, items = tasks.load_project(project)
    sources = {s.name: s for s in __import__("vidgrab.sources", fromlist=["get_sources"]).get_sources("both")}
    changed = gone = 0
    for item in items:
        before = (item.license_kind, item.repost)
        src = sources.get(item.platform)
        if not src:
            continue
        src.detail(item)
        if item.error:
            gone += 1
        if (item.license_kind, item.repost) != before:
            changed += 1
        print(f"· {item.vid:14} {pad(item.license_label or '未標示', 30)}"
              f"{'轉載' if item.repost else ''} {item.title[:40]}")
    tasks.save_project(project, manifest, items)
    print(f"\n查完 {len(items)} 支：授權結論變動 {changed} 支，取得失敗 {gone} 支")
    print(f"清單已更新：{project / 'index.html'}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    manifest, items = tasks.load_project(project)
    print(f"專案：{project}")
    print(f"關鍵字：{'、'.join(manifest.get('queries') or [])}  來源：{', '.join(manifest.get('sources') or [])}")
    print()
    print_table(items)
    print_summary(items, project)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    root = tasks.projects_root()
    projects = sorted([p for p in root.glob("*") if (p / "manifest.json").exists()])
    if not projects:
        print(f"（{root} 底下還沒有專案）")
        return 0
    print(f"{pad('專案', 36)}{'支數':>6}{'片段':>6}  關鍵字")
    for p in projects:
        manifest, items = tasks.load_project(p)
        clips = sum(len(i.clips) for i in items)
        print(f"{pad(p.name, 36)}{len(items):>6}{clips:>6}  {'、'.join(manifest.get('queries') or [])}")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    webbrowser.open(f"file://{project / 'index.html'}")
    print(project / "index.html")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    from . import config

    if args.reset:
        values = config.reset(args.reset if args.reset is not True else None)
        print(f"✔ 已還原預設（{config.config_path()}）")
        print(json.dumps(values, ensure_ascii=False, indent=2))
        return 0

    if args.set:
        patch = {}
        for item in args.set:
            if "=" not in item:
                raise SystemExit(f"要寫成 key=value，收到：{item}")
            key, _, value = item.partition("=")
            patch[key.strip()] = value.strip()
        try:
            values = config.save_patch(patch)
        except config.ConfigError as exc:
            raise SystemExit(f"✗ {exc}") from exc
        print(f"✔ 已更新：{', '.join(sorted(patch))}")
        if args.json:
            print(json.dumps(values, ensure_ascii=False, indent=2))
        return 0

    info = config.describe()
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    print(f"設定檔：{info['path']}")
    print()
    for key in sorted(info["values"]):
        value = info["values"][key]
        default = info["defaults"][key]
        mark = "" if value == default else "  (已改)"
        print(f"  {pad(key, 16)}{value!r}{mark}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """跑煙霧測試；沒裝 pytest 就給清楚的提示。"""
    import subprocess

    try:
        import pytest  # noqa: F401
    except ImportError:
        print("需要 pytest：pip install pytest（或 uv pip install -e '.[dev]'）", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[2]
    target = root / "tests"
    if not target.exists():
        print("找不到 tests/ 目錄（容器映像裡不含測試）", file=sys.stderr)
        return 2

    code = subprocess.call([sys.executable, "-m", "pytest", str(target), "-q"])

    # 順手檢查 Dockerfile：需要的檔案沒被 .dockerignore 排掉
    checker = root / "tools" / "check_docker.py"
    if checker.exists():
        print()
        rc = subprocess.call([sys.executable, str(checker)])
        if rc != 0 and code == 0:
            code = rc

    # 介面檢查要開瀏覽器，不是每次都需要；用 --ui 明確要求
    if getattr(args, "ui", False):
        ui = root / "tools" / "check_ui.py"
        if ui.exists():
            print()
            rc = subprocess.call([sys.executable, str(ui)])
            if rc != 0:
                code = rc
    return code


def cmd_build(args: argparse.Namespace) -> int:
    from .version import full_version as _full

    if args.json:
        print(json.dumps({"version": __version__, "build": build(), "full": _full()}, ensure_ascii=False))
    else:
        print(f"vidgrab {_full()}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    from . import auth

    if auth.logged_in() and not args.force:
        who = auth.whoami()
        if who.get("logged_in"):
            print(f"已經登入了：{who.get('name')}（{who.get('mid')}）")
            print(f"cookie：{auth.cookie_file()}")
            print("要換帳號請加 --force")
            return 0

    qr = auth.generate()
    png = auth.home() / "bilibili-qr.png"
    png.write_bytes(qr["png"])
    print("用手機 B 站 App →「我的」→ 右上角掃一掃，掃這張 QR：")
    print(f"  {png}")
    print("（約 180 秒內有效）")
    if not args.no_open:
        import webbrowser

        webbrowser.open(f"file://{png}")
    deadline = time.time() + 190
    last = ""
    while time.time() < deadline:
        res = auth.poll(qr["key"])
        if res["status"] != last:
            print(f"· {res['message']}")
            last = res["status"]
        if res["status"] == "ok":
            who = res.get("who") or {}
            print(f"✔ 登入成功：{who.get('name')}（{who.get('mid')}）")
            print(f"cookie 已存：{auth.cookie_file()}")
            return 0
        if res["status"] == "expired":
            print("✗ QR 過期，重跑 ./vidgrab login")
            return 1
        time.sleep(2)
    print("✗ 等太久，逾時。重跑 ./vidgrab login")
    return 1


def cmd_logout(args: argparse.Namespace) -> int:
    from . import auth

    auth.logout()
    print("已登出，cookie 已刪除。")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from .web import serve

    import os

    from . import config

    saved = config.load()
    host = args.host or os.environ.get("VIDGRAB_HOST") or saved.get("host") or "127.0.0.1"
    port = args.port or int(os.environ.get("VIDGRAB_PORT") or saved.get("port") or 8310)
    serve(host=host, port=port, open_browser=not args.no_open)
    return 0


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vidgrab",
        description="關鍵字找素材：搜 B 站／YouTube，只切你要的幾秒，連同授權資訊整理成清單。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例：
  ./vidgrab web
  ./vidgrab search 台北 街景 --per-keyword 10 --min-duration 20
  ./vidgrab search "禪修" "寺院" --platform both --order views
  ./vidgrab pick 20261105-台北 --top 3 --per 8 --frames
  ./vidgrab pick 20261105-台北 --select 2,5-6 --at 1:30 --per 12
        """,
    )
    parser.add_argument("--version", action="version", version=f"vidgrab {full_version()}")
    parser.add_argument("--debug", action="store_true", help="出錯時印出完整追蹤")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="用關鍵字搜一輪，建立素材專案")
    s.add_argument("keywords", nargs="+", help="一個或多個關鍵字（多個 = 分開搜再合併）")
    s.add_argument("--platform", choices=["both", "bilibili", "youtube"],
                   help="預設讀設定（出廠 both）")
    s.add_argument("--per-keyword", type=int, help="每個關鍵字抓幾支（出廠 12）")
    s.add_argument("--order", choices=["smart", "relevance", "views", "new"])
    s.add_argument("--duration", choices=["any", "short", "medium", "long", "verylong"],
                   help="平台端時長：short<10分、medium 10-30分、long 30-60分、verylong>60分")
    s.add_argument("--exclude", action="append", help="標題含這些詞就跳過（可重複）")
    s.add_argument("--fresh-days", type=int, help="只看最近 N 天發布")
    s.add_argument("--min-duration", type=float, help="最短幾分鐘（出廠 10）")
    s.add_argument("--max-duration", type=float, help="最長幾分鐘（0 = 不限）")
    s.add_argument("--min-views", type=int, help="至少幾個觀看")
    s.add_argument("--only-free", action="store_true", default=None, help="只留授權明確的")
    s.add_argument("--drop-reposts", action="store_true", default=None, help="丟掉轉載／非原始出處")
    s.add_argument("--verify-license", action="store_true", default=None, help="逐支抓詳情確認授權（慢，但準）")
    s.add_argument("--sort", choices=["smart", "license", "views", "new"])
    s.add_argument("--save", action="store_true",
                   help="把這次的參數存成預設值，之後不用再打")
    s.add_argument("--name", help="專案名稱（預設 日期-關鍵字）")
    s.add_argument("--out", help="專案放哪個資料夾（預設 ./projects）")
    s.add_argument("--json", action="store_true", help="輸出 JSON（給程式用）")
    s.set_defaults(func=cmd_search)

    p = sub.add_parser("pick", help="把選中的影片切成片段／抓畫面")
    p.add_argument("project", nargs="?", help="專案名稱或路徑（省略 = 最新）")
    p.add_argument("--select", help="編號，如 1,3,5-7")
    p.add_argument("--top", type=int, help="自動取排序前 N 支")
    p.add_argument("--at", action="append", help="從第幾秒開始切（可重複）；省略 = 自動挑一段")
    p.add_argument("--per", help="每段幾秒（出廠 15）")
    p.add_argument("--full", action="store_true", help="下載整支影片（不切片）")
    p.add_argument("--range", action="append", metavar="開始-結束",
                   help="自訂區段，如 1:00-2:30；可重複、也可逗號分隔多段")
    p.add_argument("--height", type=int, help="最高解析度（出廠 1080）")
    p.add_argument("--audio", action="store_true", default=None, help="只要聲音")
    p.add_argument("--audio-format", help="m4a/mp3/wav/opus（出廠 m4a）")
    p.add_argument("--format", help="自訂 yt-dlp 格式字串")
    p.add_argument("--frames", action="store_true", default=None, help="順便抓幾格畫面")
    p.add_argument("--frame-count", type=int, help="抓幾格（出廠 3）")
    p.add_argument("--frame-only", action="store_true", help="只抓畫面，不切片段")
    p.add_argument("--cookies", help="cookies.txt 路徑（B 站高畫質／會員片需要）")
    p.add_argument("--cookies-from-browser", help="例如 chrome / safari / firefox")
    p.add_argument("--dry-run", action="store_true", help="只印出會做什麼，不下載")
    p.add_argument("--open", action="store_true", help="做完自動開清單")
    p.add_argument("--save", action="store_true", help="把這次的參數（per/height/audio…）存成預設值")
    p.set_defaults(func=cmd_pick)

    r = sub.add_parser("refresh", help="回頭確認授權與影片還在不在")
    r.add_argument("project", nargs="?")
    r.set_defaults(func=cmd_refresh)

    sh = sub.add_parser("show", help="看專案清單")
    sh.add_argument("project", nargs="?")
    sh.set_defaults(func=cmd_show)

    l = sub.add_parser("list", help="列出所有專案")
    l.set_defaults(func=cmd_list)

    o = sub.add_parser("open", help="用瀏覽器打開清單")
    o.add_argument("project", nargs="?")
    o.set_defaults(func=cmd_open)

    cfg = sub.add_parser("config", help="看／改預設設定（存過就一直是預設）")
    cfg.add_argument("--set", action="append", metavar="KEY=VALUE",
                     help="改設定，可重複，如 --set per=30 --set height=720")
    cfg.add_argument("--reset", nargs="*", metavar="KEY",
                     help="還原預設（不給 key = 全部還原）")
    cfg.add_argument("--json", action="store_true")
    cfg.set_defaults(func=cmd_config)

    tst = sub.add_parser("test", help="跑煙霧測試")
    tst.add_argument("--ui", action="store_true",
                     help="另外用真的 Chrome 驗證介面（需要 Chrome 在 9222 開 CDP）")
    tst.set_defaults(func=cmd_test)

    bd = sub.add_parser("build", help="顯示版本與 build number")
    bd.add_argument("--json", action="store_true", help="輸出 JSON")
    bd.set_defaults(func=cmd_build)

    lg = sub.add_parser("login", help="掃碼登入 B 站（存 cookie，高畫質與會員片必備）")
    lg.add_argument("--force", action="store_true", help="已經登入也重新登入")
    lg.add_argument("--no-open", action="store_true", help="不要自動開圖（容器／無圖形介面用）")
    lg.set_defaults(func=cmd_login)

    lo = sub.add_parser("logout", help="登出 B 站並刪除 cookie")
    lo.set_defaults(func=cmd_logout)

    w = sub.add_parser("web", help="開 Web UI（本機網頁介面）")
    w.add_argument("--host", default=None, help="預設讀 VIDGRAB_HOST，否則 127.0.0.1")
    w.add_argument("--port", type=int, default=None, help="預設讀 VIDGRAB_PORT，否則 8310")
    w.add_argument("--no-open", action="store_true", help="不要自動開瀏覽器")
    w.set_defaults(func=cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not media.ffmpeg_ok() and args.cmd != "list":
        print("✗ 需要 ffmpeg 與 ffprobe：brew install ffmpeg", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n中止。", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        if args.debug:
            traceback.print_exc()
        print(f"✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
