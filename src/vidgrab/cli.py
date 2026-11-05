"""命令列介面：search / pick / refresh / show / list / open / web。"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import webbrowser
from pathlib import Path

from . import __version__, media, report, tasks
from .models import (
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


def cmd_search(args: argparse.Namespace) -> int:
    result = tasks.run_search(
        args.keywords,
        platform=args.platform,
        per_keyword=args.per_keyword,
        order=args.order,
        duration=args.duration,
        exclude=args.exclude,
        fresh_days=args.fresh_days,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        min_views=args.min_views,
        only_free=args.only_free,
        drop_reposts=args.drop_reposts,
        verify_license=args.verify_license,
        sort=args.sort,
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

    from .models import parse_ts

    result = tasks.run_pick(
        project,
        idxs,
        per=float(args.per),
        starts=[parse_ts(a) for a in (args.at or [])],
        frames=args.frames,
        frame_count=args.frame_count,
        height=args.height,
        audio=args.audio,
        audio_format=args.audio_format,
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
    if args.open or True:
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

    serve(host=args.host, port=args.port, open_browser=not args.no_open)
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
    parser.add_argument("--version", action="version", version=f"vidgrab {__version__}")
    parser.add_argument("--debug", action="store_true", help="出錯時印出完整追蹤")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="用關鍵字搜一輪，建立素材專案")
    s.add_argument("keywords", nargs="+", help="一個或多個關鍵字（多個 = 分開搜再合併）")
    s.add_argument("--platform", default="both", choices=["both", "bilibili", "youtube"])
    s.add_argument("--per-keyword", type=int, default=12, help="每個關鍵字抓幾支（預設 12）")
    s.add_argument("--order", default="smart", choices=["smart", "relevance", "views", "new"])
    s.add_argument("--duration", default="any",
                   choices=["any", "short", "medium", "long", "verylong"],
                   help="平台端時長：short<10分、medium 10-30分、long 30-60分、verylong>60分")
    s.add_argument("--exclude", action="append", help="標題含這些詞就跳過（可重複）")
    s.add_argument("--fresh-days", type=int, default=0, help="只看最近 N 天發布")
    s.add_argument("--min-duration", type=float, default=10, help="最短幾分鐘（預設 10）")
    s.add_argument("--max-duration", type=float, default=0, help="最長幾分鐘（0 = 不限）")
    s.add_argument("--min-views", type=int, default=0, help="至少幾個觀看")
    s.add_argument("--only-free", action="store_true", help="只留授權明確的")
    s.add_argument("--drop-reposts", action="store_true", help="丟掉轉載／非原始出處")
    s.add_argument("--verify-license", action="store_true", help="逐支抓詳情確認授權（慢，但準）")
    s.add_argument("--sort", default="smart", choices=["smart", "license", "views", "new"])
    s.add_argument("--name", help="專案名稱（預設 日期-關鍵字）")
    s.add_argument("--out", help="專案放哪個資料夾（預設 ./projects）")
    s.add_argument("--json", action="store_true", help="輸出 JSON（給程式用）")
    s.set_defaults(func=cmd_search)

    p = sub.add_parser("pick", help="把選中的影片切成片段／抓畫面")
    p.add_argument("project", nargs="?", help="專案名稱或路徑（省略 = 最新）")
    p.add_argument("--select", help="編號，如 1,3,5-7")
    p.add_argument("--top", type=int, help="自動取排序前 N 支")
    p.add_argument("--at", action="append", help="從第幾秒開始切（可重複）；省略 = 自動挑一段")
    p.add_argument("--per", default="15", help="每段幾秒（預設 15）")
    p.add_argument("--height", type=int, default=1080, help="最高解析度（預設 1080）")
    p.add_argument("--audio", action="store_true", help="只要聲音")
    p.add_argument("--audio-format", default="m4a", help="m4a/mp3/wav/opus")
    p.add_argument("--format", help="自訂 yt-dlp 格式字串")
    p.add_argument("--frames", action="store_true", help="順便抓幾格畫面（不必下載整支）")
    p.add_argument("--frame-count", type=int, default=3, help="抓幾格（預設 3）")
    p.add_argument("--frame-only", action="store_true", help="只抓畫面，不切片段")
    p.add_argument("--cookies", help="cookies.txt 路徑（B 站高畫質／會員片需要）")
    p.add_argument("--cookies-from-browser", help="例如 chrome / safari / firefox")
    p.add_argument("--dry-run", action="store_true", help="只印出會做什麼，不下載")
    p.add_argument("--open", action="store_true", help="做完自動開清單")
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

    lg = sub.add_parser("login", help="掃碼登入 B 站（存 cookie，高畫質與會員片必備）")
    lg.add_argument("--force", action="store_true", help="已經登入也重新登入")
    lg.set_defaults(func=cmd_login)

    lo = sub.add_parser("logout", help="登出 B 站並刪除 cookie")
    lo.set_defaults(func=cmd_logout)

    w = sub.add_parser("web", help="開 Web UI（本機網頁介面）")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=7788)
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
