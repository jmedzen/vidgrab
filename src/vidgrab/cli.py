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
    """找出專案目錄。

    失敗時丟 `NoProject`／`NotFound`（不是 SystemExit），
    這樣 JSON 模式才能回結構化錯誤給 agent。
    """
    from .models import NoProject, NotFound

    if not value:
        cands = sorted([p for p in tasks.projects_root().glob("*") if (p / "manifest.json").exists()])
        if not cands:
            raise NoProject(f"{tasks.projects_root()} 底下還沒有專案")
        log(f"（沒指定專案，用最新的：{cands[-1].name}）")
        return cands[-1]
    path = Path(value).expanduser()
    if path.is_absolute():
        if (path / "manifest.json").exists():
            return path
        raise NotFound(f"找不到專案（沒有 manifest.json）：{path}")
    if (Path.cwd() / path / "manifest.json").exists():
        return Path.cwd() / path
    candidate = tasks.projects_root() / value
    if (candidate / "manifest.json").exists():
        return candidate
    matches = [p for p in tasks.projects_root().glob(f"*{value}*") if (p / "manifest.json").exists()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        from .models import BadInput

        raise BadInput(
            "對到多個專案：" + "、".join(m.name for m in matches),
            code="ambiguous_project",
            hint="改用具名的完整專案名稱",
        )
    raise NotFound(f"找不到專案：{value}")


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
        replace=getattr(args, "replace", False),
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
                from .models import BadInput

                raise BadInput(
                    f"區段要寫成 開始-結束（收到 {chunk}）",
                    hint="例如 --range 1:00-2:30",
                )
            lo, _, hi = chunk.partition("-")
            start, end = _ts(lo), _ts(hi)
            if end <= start:
                from .models import BadInput

                raise BadInput(f"區段結束要比開始晚：{chunk}")
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
        from .models import BadInput

        raise BadInput("沒有指定要抓哪幾支", hint="用 --select 1,3-5 或 --top 3")
    if not idxs:
        from .models import BadInput

        raise BadInput("選到的編號都是空的", hint="確認編號沒有超出範圍")

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


def cmd_scrub(args: argparse.Namespace) -> int:
    """清掉既有資料裡的技術標記；不重新抓取，純本機整理。"""
    root = tasks.projects_root()
    targets = (
        [resolve_project(args.project)] if args.project
        else [p for p in sorted(root.glob("*")) if (p / "manifest.json").exists()]
    )
    total = 0
    for project in targets:
        n = tasks.scrub_metadata(project)
        total += n
        print(f"· {project.name}: 清掉 {n} 支的技術標記")
    print(f"\n共 {len(targets)} 個專案，{total} 支有變動")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    """一次抓多個片段——agent 的批次入口。

    三個關鍵性質：
    - **冪等**：同樣的請求重跑不會重複下載，回 `cached: true`
    - **部分成功**：某支失敗不影響其他支，每支各自回報結果
    - **可機器判讀**：一律輸出 JSON，含 exit code 對應的成功／失敗數
    """
    from . import agent, config

    project = resolve_project(args.project)

    # 解析要抓什麼：可以用 asset id、索引，或「這個專案全部」
    manifest, items = tasks.load_project(project)
    targets: list[int] = []
    if args.select:
        targets = parse_selector(args.select, len(items))
    elif args.top:
        targets = list(range(min(args.top, len(items))))
    elif args.all:
        targets = list(range(len(items)))
    if not targets:
        emit_json({"error": {"code": "no_targets",
                             "message": "沒有指定要抓的項目（--select／--top／--all）",
                             "retryable": False}})
        return 1

    values = effective(args, ("per", "height", "audio", "audio_format"))
    if args.save:
        patch = {k: v for k, v in values.items() if getattr(args, k, None) is not None}
        config.save_patch(patch)

    out = agent.run_batch(
        project, targets,
        timeout=float(args.timeout) if args.timeout else None,
        per=float(values["per"]),
        ranges=_parse_ranges(args.range or []),
        full=bool(args.full),
        height=int(values["height"]),
        audio=bool(values["audio"]),
        audio_format=str(values["audio_format"]),
        frames=bool(args.frames),
        frame_count=int(args.frame_count),
        dry_run=bool(args.dry_run),
    )  # emit 不給：stdout 只能有 JSON
    emit_json(out)
    return 0 if out["failed"] == 0 and not out["timed_out"] else 1


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


def emit_json(payload: dict | list) -> None:
    """機器可讀輸出。agent 靠這個，不要印任何其他人類文字到 stdout。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_thumbs(args: argparse.Namespace) -> int:
    """幫已下載的片段補上縮圖（縮圖功能是後來才加的）。

    縮圖一律從片段檔本身抽，所以畫面必然對得上。
    """
    targets = []
    if args.projects:
        for raw in args.projects:
            targets.append(resolve_project(raw))
    else:
        root = tasks.projects_root()
        if root.exists():
            targets = sorted(p for p in root.iterdir()
                             if (p / "results.json").exists())

    out = []
    for proj in targets:
        result = tasks.backfill_thumbs(proj, emit=lambda *_: None)
        out.append({"project": proj.name, **result})

    emit_json({
        "schema": "vidgrab.thumbs/1",
        "projects": out,
        "made": sum(o["made"] for o in out),
        "kept": sum(o["kept"] for o in out),
        "failed": sum(o["failed"] for o in out),
    })
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    """把專案從素材庫清單移除（資料夾已刪掉、清單還留著記錄時用）。

    預設不自動移除——資料夾暫時讀不到不該讓清單消失。
    所以要移除得自己講。
    """
    from . import library

    lib = library.load()
    known = set(lib.get("items") or {})
    # 這裡**不能**用 resolve_project：它要求資料夾真的存在，
    # 而 forget 正是用來處理「資料夾已經刪掉、清單還留著」的情況。
    names = []
    for raw in args.projects:
        name = Path(raw).name if Path(raw).exists() else raw
        names.append(name)

    gone = [n for n in names if n not in known]
    lib = library.forget(lib, names)
    library.save(lib)

    emit_json({
        "schema": "vidgrab.forget/1",
        "removed": [n for n in names if n not in gone],
        "not_found": gone,
        "remaining": len(lib.get("items") or {}),
    })
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """清掉磁碟上沒被清單追蹤的孤兒檔案（purge 之後、或手動刪清單留下的）。

    不做的話素材庫會越用越肥：清單只剩 2 支，磁碟上還躺著 7 個檔。
    """
    from . import agent

    project = resolve_project(args.project)
    manifest, items = tasks.load_project(project)
    clips_dir = project / "clips"

    tracked = {Path(c["path"]).name for it in items for c in it.clips}
    orphans = []
    if clips_dir.exists():
        for p in sorted(clips_dir.iterdir()):
            if not p.is_file():
                continue
            # info.json 跟著 mp4 一起判斷，避免留下孤兒 sidecar
            stem_mp4 = p.name[:-10] if p.name.endswith(".info.json") else p.name
            if stem_mp4 not in tracked and p.name not in tracked:
                orphans.append(p)

    freed = sum(p.stat().st_size for p in orphans)

    # 另一種孤兒：清單裡有紀錄、磁碟上卻沒有檔案。
    # 卡片會顯示成「看得到、點不開」，比檔案孤兒更煩人。
    stale: list[tuple[int, str]] = []
    for idx, it in enumerate(items):
        keep = []
        for c in it.clips:
            path = Path(c.get("path") or (clips_dir / Path(str(c.get("name") or "")).name))
            if path.exists():
                keep.append(c)
            else:
                stale.append((idx, path.name))
        it.clips = keep

    if not args.dry_run:
        for p in orphans:
            try:
                p.unlink()
            except OSError:
                pass
        if stale:
            tasks.save_project(project, manifest, items)

    emit_json({
        "schema": "vidgrab.prune/1",
        "project": project.name,
        "orphans": len(orphans),
        "stale_records": len(stale),
        "freed_bytes": freed,
        "freed_human": __import__("vidgrab.models", fromlist=["human_size"]).human_size(freed),
        "dry_run": bool(args.dry_run),
        "files": [p.name for p in orphans],
        "stale": [n for _i, n in stale],
    })
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    """把清單裡的教學／課程篩掉。過濾規則改了以後，舊專案可以用它重新整理。"""
    from . import agent
    from .models import looks_like_tutorial

    project = resolve_project(args.project)
    manifest, items = tasks.load_project(project)

    dropped = [i for i in items if looks_like_tutorial(i)]
    if not args.dry_run and dropped:
        keep = [i for i in items if not looks_like_tutorial(i)]
        tasks.save_project(project, manifest, keep)
        items = keep

    emit_json({
        "schema": "vidgrab.purge/1",
        "project": project.name,
        "dropped": 0 if args.dry_run else len(dropped),
        "would_drop": len(dropped),
        "remaining": len(items),
        "dry_run": bool(args.dry_run),
        "titles": [i.title for i in dropped],
    })
    return 0


def cmd_vault(args: argparse.Namespace) -> int:
    """整個素材庫的快照——agent 的主要入口。"""
    from . import agent

    project = resolve_project(args.project).name if args.project else ""
    emit_json(agent.vault_query(
        project=project, query=args.query or "", limit=args.limit,
        with_items=not args.no_items, with_description=args.with_description,
    ))
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    """用片段 id 或 asset id 找回素材。"""
    from . import agent

    hit = agent.find_by_id(args.id)
    if hit is None:
        emit_json({"error": {"code": "not_found", "message": f"找不到：{args.id}",
                             "retryable": False}})
        return 1
    emit_json(hit)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    if getattr(args, "json", False):
        from . import agent

        emit_json(agent.project_summary(project, with_items=True, with_description=True))
        return 0
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
    if getattr(args, "json", False):
        from . import agent

        emit_json(agent.vault_manifest(with_items=False))
        return 0
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
                from .models import BadInput

            raise BadInput(f"要寫成 key=value，收到：{item}")
            key, _, value = item.partition("=")
            patch[key.strip()] = value.strip()
        try:
            values = config.save_patch(patch)
        except config.ConfigError as exc:
            raise BadInput(str(exc)) from exc
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

    # 前端產物有沒跟上原始碼（改了 web/src 卻忘記 build 是最容易漏的一步）
    frontend = root / "tools" / "check_frontend.sh"
    if frontend.exists():
        print()
        rc = subprocess.call(["bash", str(frontend)])
        if rc != 0 and code == 0:
            code = rc

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
        if not ui.exists():
            print("! 找不到 tools/check_ui.py，略過介面檢查")
        else:
            print()
            # --launch：沒偵測到 CDP 就自己起一個臨時 Chromium。
            # 注意這不是「幫你開瀏覽器看介面」——vidgrab 開介面用的是系統預設瀏覽器，
            # 這裡只是為了能下斷言而需要一個可遙控的 Chromium。
            # --fixture：用假的素材庫起一個獨立實例，
            # 這樣不會依賴、也不會弄壞真人正在用的 projects/
            rc = subprocess.call([sys.executable, str(ui), "--launch", "--fixture"])
            if rc == 2:
                print("! 介面檢查需要 Chromium 系瀏覽器（CDP），這台機器上找不到，已略過")
            elif rc != 0:
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


def cmd_mcp(args: argparse.Namespace) -> int:
    """MCP server（stdio）。HTTP 版本在 `vidgrab web` 的 POST /mcp。"""
    from .mcp import serve_stdio

    return serve_stdio()


def cmd_web(args: argparse.Namespace) -> int:
    from .web import serve

    import os

    from . import config

    saved = config.load()
    host = args.host or os.environ.get("VIDGRAB_HOST") or saved.get("host") or "127.0.0.1"
    port = args.port or int(os.environ.get("VIDGRAB_PORT") or saved.get("port") or 8310)

    # 這個服務沒有任何登入機制，綁到 0.0.0.0 等於把素材庫、
    # 下載能力、B 站登入狀態開放給整個區網。要這樣做必須自己明講。
    exposed = host not in ("127.0.0.1", "localhost", "::1")
    if exposed and not getattr(args, "allow_lan", False):
        sys.stderr.write(
            f"✗ 拒絕綁定到 {host}：這個介面沒有登入機制。\n"
            "  綁到區網等於讓同網段的任何人下載／修改你的素材。\n"
            "  真的要這樣做，加 --allow-lan 表示你知道風險。\n"
        )
        return 2
    from .web import TOKEN_ENV, api_token

    if exposed and not api_token():
        if not (getattr(args, "no_auth", False) or os.environ.get("VIDGRAB_NO_AUTH") == "1"):
            sys.stderr.write(
                f"✗ 拒絕綁定到 {host}：沒有設定 {TOKEN_ENV}。\n"
                f"  請設定 token 再啟動（例如 export {TOKEN_ENV}=$(openssl rand -hex 24)），\n"
                "  agent 用 Authorization: Bearer <token>，瀏覽器開 /?token=<token> 登入。\n"
                "  確定外層已經有保護（例如只對 127.0.0.1 發佈 port）才用 --no-auth 略過。\n"
            )
            return 2
        sys.stderr.write(f"⚠ 綁定到 {host} 且沒有 token（--no-auth）：能連到 port 的人都能操作。\n")
    elif exposed:
        sys.stderr.write(f"▸ 綁定到 {host}，API 需要 token。\n")

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
    s.add_argument(
        "--replace", action="store_true",
        help="同名專案已存在時直接覆蓋（預設會另開一個帶時間戳的，避免蓋掉已下載的片段）",
    )
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

    sc = sub.add_parser("scrub", help="清掉既有資料裡的技術標記（不重新抓取）")
    sc.add_argument("project", nargs="?", help="專案名稱或路徑（省略 = 全部）")
    sc.set_defaults(func=cmd_scrub)

    bt = sub.add_parser("batch", help="批次抓片（agent 用，一律 JSON）")
    bt.add_argument("project", nargs="?", help="專案名稱或路徑（省略 = 最新）")
    bt.add_argument("--select", help="編號，如 1,3,5-7")
    bt.add_argument("--top", type=int, help="前 N 支")
    bt.add_argument("--all", action="store_true", help="整個專案")
    bt.add_argument("--range", action="append", help="區段，如 1:00-2:30")
    bt.add_argument("--full", action="store_true", help="整支下載")
    bt.add_argument("--per", type=float, help="每段幾秒")
    bt.add_argument("--height", type=int, help="最高解析度")
    bt.add_argument("--audio", action="store_true", help="只要聲音")
    bt.add_argument("--audio-format", dest="audio_format", help="m4a/mp3/wav/opus")
    bt.add_argument("--frames", action="store_true", help="順便抓畫面")
    bt.add_argument("--frame-count", dest="frame_count", type=int, default=3)
    bt.add_argument("--dry-run", action="store_true", help="只回報要抓什麼，不下載")
    bt.add_argument("--save", action="store_true", help="把這次的選項存成預設")
    bt.add_argument("--timeout", type=float, help="這個批次最多跑幾秒（超過就中止）")
    bt.set_defaults(func=cmd_batch)

    pt = sub.add_parser("thumbs", help="幫已下載的片段補縮圖（省略專案＝全部）")
    pt.add_argument("projects", nargs="*", help="專案名稱（省略＝所有專案）")
    pt.set_defaults(func=cmd_thumbs)

    pf = sub.add_parser("forget", help="把專案從素材庫清單移除（資料夾已刪掉時用）")
    pf.add_argument("projects", nargs="+", help="專案名稱（可多個）")
    pf.set_defaults(func=cmd_forget)

    pn = sub.add_parser("prune", help="清掉磁碟上沒被清單追蹤的孤兒檔案")
    pn.add_argument("project", nargs="?", help="專案名稱或路徑（省略 = 最新）")
    pn.add_argument("--dry-run", action="store_true", help="只回報會刪什麼")
    pn.set_defaults(func=cmd_prune)

    pg = sub.add_parser("purge", help="篩掉清單裡的教學／課程")
    pg.add_argument("project", nargs="?", help="專案名稱或路徑（省略 = 最新）")
    pg.add_argument("--dry-run", action="store_true", help="只回報會刪什麼，不動資料")
    pg.set_defaults(func=cmd_purge)

    vt = sub.add_parser("vault", help="素材庫快照（agent 的主要入口）")
    vt.add_argument("project", nargs="?", help="只看某個專案")
    vt.add_argument("--query", help="在已下載的素材裡搜尋")
    vt.add_argument("--limit", type=int, default=20, help="搜尋回幾筆（出廠 20）")
    vt.add_argument("--no-items", action="store_true", help="只回專案摘要，不回每支素材")
    vt.add_argument("--with-description", action="store_true", help="附上完整說明")
    vt.set_defaults(func=cmd_vault)

    fd = sub.add_parser("find", help="用 asset id 或片段 id 找回素材")
    fd.add_argument("id", help="例如 bilibili:BV1xx 或 bilibili:BV1xx#60-75")
    fd.set_defaults(func=cmd_find)

    sh = sub.add_parser("show", help="看專案清單")
    sh.add_argument("project", nargs="?")
    sh.add_argument("--json", action="store_true", help="輸出 JSON（給程式／agent 用）")
    sh.set_defaults(func=cmd_show)

    l = sub.add_parser("list", help="列出所有專案")
    l.add_argument("--json", action="store_true", help="輸出 JSON（給程式／agent 用）")
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
                     help="另外驗證介面（會自動起一個臨時的 Chromium，跑完關掉）")
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

    mc = sub.add_parser("mcp", help="MCP server（stdio），給 Claude Code 等 MCP client 用")
    mc.set_defaults(func=cmd_mcp)

    w = sub.add_parser("web", help="開 Web UI（本機網頁介面）")
    w.add_argument("--host", default=None, help="預設讀 VIDGRAB_HOST，否則 127.0.0.1")
    w.add_argument(
        "--allow-lan", action="store_true",
        help="允許綁定到非本機位址（這個介面沒有登入機制，開放區網請自行評估）",
    )
    w.add_argument(
        "--no-auth", action="store_true",
        help="綁非本機位址又不設 VIDGRAB_API_TOKEN 時必須加這個（也可設 VIDGRAB_NO_AUTH=1）",
    )
    w.add_argument("--port", type=int, default=None, help="預設讀 VIDGRAB_PORT，否則 8310")
    w.add_argument("--no-open", action="store_true", help="不要自動開瀏覽器")
    w.set_defaults(func=cmd_web)

    return parser


def _json_mode(args: argparse.Namespace) -> bool:
    """這個指令是不是在「機器可讀」模式。

    agent 靠這個判斷：JSON 模式下，成功與失敗都只能有 JSON 到 stdout。
    """
    if getattr(args, "json", False):
        return True
    return getattr(args, "cmd", "") in ("vault", "find", "batch")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    machine = _json_mode(args)

    from .models import MissingFFmpeg, VidgrabError

    from .models import NEEDS_NO_FFMPEG

    if not media.ffmpeg_ok() and args.cmd not in NEEDS_NO_FFMPEG:
        err = MissingFFmpeg("需要 ffmpeg 與 ffprobe")
        if machine:
            emit_json({"ok": False, "error": err.to_dict()})
        else:
            print(f"✗ {err}：{err.hint}", file=sys.stderr)
        return 2

    try:
        return args.func(args)
    except KeyboardInterrupt:
        if machine:
            emit_json({"ok": False, "error": {
                "code": "interrupted", "message": "使用者中斷", "retryable": True}})
        else:
            print("\n中止。", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except VidgrabError as exc:
        # 可預期錯誤：給 agent 判斷該不該重試
        if args.debug:
            traceback.print_exc()
        if machine:
            emit_json({"ok": False, "error": exc.to_dict()})
        else:
            print(f"✗ {exc}", file=sys.stderr)
            if exc.hint:
                print(f"  → {exc.hint}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        if args.debug:
            traceback.print_exc()
        if machine:
            # 非預期錯誤也要是合法 JSON，agent 才不會拿到一行 traceback
            emit_json({"ok": False, "error": {
                "code": "internal",
                "message": f"{type(exc).__name__}: {exc}",
                "retryable": True,
                "hint": "這是未預期的錯誤，帶 --debug 可以看到細節",
            }})
        else:
            print(f"✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
