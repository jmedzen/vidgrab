"""把一輪搜尋的結果變成看得懂、給得出去的清單。"""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any

from .models import Item, fmt_duration, fmt_ts, human_size, LICENSE_LABELS

PLATFORM_LABEL = {"bilibili": "B 站", "youtube": "YouTube"}
LICENSE_CLASS = {
    "free": "lic-free",
    "free-attribution": "lic-free",
    "check": "lic-check",
    "unknown": "lic-unknown",
    "none": "lic-unknown",
}


# ---------------------------------------------------------------- 拼版


# ---------------------------------------------------------------- 挑選


def rank(items: list[Item], sort: str = "smart") -> list[Item]:
    license_rank = {"free": 0, "free-attribution": 1, "check": 2, "unknown": 3, "none": 4}

    if sort == "views":
        return sorted(items, key=lambda i: i.views or 0, reverse=True)
    if sort == "new":
        return sorted(items, key=lambda i: i.pubdate or "", reverse=True)
    if sort == "license":
        return sorted(items, key=lambda i: (license_rank.get(i.license_kind, 9), -(i.views or 0)))
    # smart：授權乾淨的優先，再看實際有沒有被看過、長度合不合剪輯用
    def score(i: Item) -> tuple:
        lic = license_rank.get(i.license_kind, 9)
        repost = 1 if i.repost else 0
        has_media = 1 if i.clips else 0
        return (lic, repost, -has_media, -(i.views or 0))

    return sorted(items, key=score)


def summarize(items: list[Item]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.license_kind] = counts.get(item.license_kind, 0) + 1
    return {
        "total": len(items),
        "by_platform": {
            p: sum(1 for i in items if i.platform == p) for p in ("bilibili", "youtube")
        },
        "by_license": counts,
        "reposts": sum(1 for i in items if i.repost),
        "with_clips": sum(1 for i in items if i.clips),
        "clip_bytes": sum(c.get("size_bytes") or 0 for i in items for c in i.clips),
    }


# ---------------------------------------------------------------- CSV


def write_csv(path: Path, items: list[Item], root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "#", "平台", "標題", "作者", "頻道", "作者頁面", "時長", "解析度",
                "觀看數", "發布日", "授權", "授權名稱", "轉載",
                "檔案", "連結", "命中關鍵字", "素材關鍵字", "分類", "標註建議", "備註",
            ]
        )
        for idx, item in enumerate(items, 1):
            files = "; ".join(
                _rel(Path(c["path"]), root) + f" [{span_label(c)}]" for c in item.clips
            )
            from .models import contact_block, search_terms

            res = f"{item.width}x{item.height}" if item.width and item.height else ""
            writer.writerow(
                [
                    idx,
                    PLATFORM_LABEL.get(item.platform, item.platform),
                    item.title,
                    item.author,
                    item.channel,
                    item.author_url,
                    fmt_duration(item.duration),
                    res,
                    item.views or "",
                    item.pubdate,
                    item.license_label or "",
                    item.license_name,
                    "是" if item.repost else "",
                    files,
                    item.webpage_url or item.url,
                    item.keyword,
                    " ".join(search_terms(item)),
                    " / ".join(item.categories),
                    contact_block(item)["credit_hint"],
                    item.error or item.check_note,
                ]
            )


# ---------------------------------------------------------------- HTML


def span_label(clip: dict[str, Any]) -> str:
    """整支下載沒有起訖時間，顯示成「整支」。"""
    start, end = clip.get("start"), clip.get("end")
    if start is None or end is None:
        return "整支"
    return f"{fmt_ts(start)}–{fmt_ts(end)}"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:  # noqa: BLE001
        return str(path)


def write_html(path: Path, items: list[Item], root: Path, meta: dict[str, Any]) -> None:
    stats = summarize(items)
    cards = "\n".join(_card(i, idx, root) for idx, i in enumerate(items, 1))
    queries = "、".join(meta.get("queries") or [])
    sources = "＋".join(PLATFORM_LABEL.get(s, s) for s in meta.get("sources") or [])
    lic_rows = "".join(
        f"<span class='pill {LICENSE_CLASS.get(k, 'lic-unknown')}'>"
        f"{LICENSE_LABELS.get(k, k)} · {v}</span>"
        for k, v in sorted(stats["by_license"].items())
    )

    doc = f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>素材清單 · {html.escape(queries)}</title>
<style>
  :root {{
    --bg:#0f1115; --panel:#171a21; --line:#262b36; --text:#e8eaf0;
    --muted:#98a0b3; --accent:#7cc4ff; --ok:#6ee7a8; --warn:#f5c66b; --bad:#ff8f8f;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; padding:32px 24px 64px; background:var(--bg); color:var(--text);
    font:15px/1.65 -apple-system,"PingFang TC","Noto Sans TC",system-ui,sans-serif;
  }}
  .wrap {{ max-width:1180px; margin:0 auto; }}
  h1 {{ font-size:24px; margin:0 0 6px; letter-spacing:.02em; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:22px; }}
  .pills {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:26px; }}
  .pill {{
    font-size:12px; padding:4px 10px; border-radius:999px;
    border:1px solid var(--line); background:var(--panel); color:var(--muted);
  }}
  .lic-free {{ color:var(--ok); border-color:#245c42; }}
  .lic-check {{ color:var(--warn); border-color:#5a4a24; }}
  .lic-unknown {{ color:var(--bad); border-color:#5c3030; }}
  .card {{
    background:var(--panel); border:1px solid var(--line); border-radius:14px;
    padding:16px 18px; margin-bottom:14px;
  }}
  .card h2 {{ font-size:16px; margin:0 0 8px; font-weight:600; }}
  .card h2 a {{ color:var(--text); text-decoration:none; }}
  .card h2 a:hover {{ color:var(--accent); }}
  .no {{ color:var(--muted); font-variant-numeric:tabular-nums; margin-right:8px; }}
  .meta {{ color:var(--muted); font-size:12.5px; display:flex; flex-wrap:wrap; gap:14px; }}
  .meta b {{ color:var(--text); font-weight:600; }}
  .frames {{ display:flex; gap:6px; margin:10px 0 4px; overflow-x:auto; }}
  .frames img {{
    width:220px; height:124px; object-fit:cover; border-radius:8px;
    border:1px solid var(--line); background:#000;
  }}
  .note {{ font-size:12.5px; color:var(--warn); margin-top:6px; }}
  .clips {{ margin-top:10px; font-size:13px; }}
  .clips a {{ color:var(--accent); text-decoration:none; }}
  .clips code {{ background:#0b0d12; padding:1px 6px; border-radius:5px; }}
  .empty {{ color:var(--muted); }}
  footer {{ color:var(--muted); font-size:12px; margin-top:36px; border-top:1px solid var(--line); padding-top:14px; }}
</style>
</head>
<body><div class="wrap">
<h1>素材清單：{html.escape(queries)}</h1>
<div class="sub">
  來源 {html.escape(sources)} · 共 {stats['total']} 支候選 · 已切 {stats['with_clips']} 段
  （{human_size(stats['clip_bytes'])}）· 產生於 {html.escape(meta.get('created', ''))}
</div>
<div class="pills">{lic_rows}</div>
{cards}
<footer>
  授權欄位是從標題／說明／平台欄位推測的，<b>不能當法律依據</b>。
  要公開發布或教學使用，請點進原片確認條款，並在影片說明標明出處、作者與授權。
  沒有明確授權的素材，合理引用短片段（評論、教學）風險較低，但仍需標明來源。
</footer>
</div></body></html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")


def _card(item: Item, idx: int, root: Path) -> str:
    frames = "".join(
        f"<img src='{html.escape(_rel(Path(f), root))}' alt='畫面 {n + 1}' loading='lazy'>"
        for n, f in enumerate(item.thumbs)
    )
    frame_block = f"<div class='frames'>{frames}</div>" if frames else ""
    clips = ""
    if item.clips:
        rows = []
        for clip in item.clips:
            rel = _rel(Path(clip["path"]), root)
            size = human_size(clip.get("size_bytes") or 0)
            dims = f"{clip.get('width')}×{clip.get('height')}" if clip.get("width") else ""
            span = span_label(clip)
            rows.append(
                f"<a href='{html.escape(rel)}'>{html.escape(clip.get('label') or Path(rel).name)}</a>"
                f" <code>{html.escape(span)}</code>"
                f" <span class='empty'>{dims} · {size}</span>"
            )
        clips = "<div class='clips'>已下載：" + "<br>".join(rows) + "</div>"
    notes = []
    if item.repost:
        notes.append("轉載／非原作者上傳")
    if item.check_note:
        notes.append(item.check_note)
    if item.error:
        notes.append(item.error)
    note_block = f"<div class='note'>{html.escape('；'.join(notes))}</div>" if notes else ""

    return f"""<div class="card">
  <h2><span class="no">{idx:02d}</span><a href="{html.escape(item.url)}" target="_blank" rel="noopener">{html.escape(item.title)}</a></h2>
  <div class="meta">
    <span>{PLATFORM_LABEL.get(item.platform, item.platform)}</span>
    <span>{html.escape(item.author or '—')}</span>
    <span>時長 <b>{fmt_duration(item.duration)}</b></span>
    <span>觀看 <b>{item.views if item.views is not None else '—'}</b></span>
    <span>{html.escape(item.pubdate or '—')}</span>
    <span class="pill {LICENSE_CLASS.get(item.license_kind, 'lic-unknown')}">{html.escape(item.license_label or '未標示')}</span>
    <span>關鍵字：{html.escape(item.keyword or '—')}</span>
  </div>
  {frame_block}
  {note_block}
  {clips}
</div>"""


def write_readme(path: Path, items: list[Item], meta: dict[str, Any]) -> None:
    stats = summarize(items)
    lines = [
        f"# 素材清單：{'、'.join(meta.get('queries') or [])}",
        "",
        f"- 產生時間：{meta.get('created', '')}",
        f"- 來源：{'、'.join(PLATFORM_LABEL.get(s, s) for s in meta.get('sources') or [])}",
        f"- 候選 {stats['total']} 支，已切片段 {stats['with_clips']} 段（{human_size(stats['clip_bytes'])}）",
        "",
        "## 怎麼看",
        "",
        "1. 先開 `index.html`（雙擊即可），一眼掃畫面和授權。",
        "2. `clips/` 裡就是已經切好的片段，直接拖進剪輯軟體。",
        "3. `clips.csv` 可以丟進試算表做篩選。",
        "",
        "## 授權提醒",
        "",
        "清單裡的授權標記是自動推測的，**不能當法律依據**。公開使用前請：",
        "",
        "- 點回原片確認授權條款；",
        "- 在影片說明標明出處、作者與授權方式；",
        "- 沒有明確授權的素材，只用短片段並以評論／教學形式引用，風險較低，但一樣要標來源。",
        "",
        "## 這一輪的影片",
        "",
    ]
    for idx, item in enumerate(items, 1):
        lines.append(
            f"{idx}. {item.title} — {item.author or '—'} · {fmt_duration(item.duration)} · "
            f"{item.license_label or '未標示'} · {item.url}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def rebuild(project: Path) -> None:
    manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
    items = [Item(**raw) for raw in json.loads((project / "results.json").read_text(encoding="utf-8"))]
    write_html(project / "index.html", items, project, manifest)
    write_csv(project / "clips.csv", items, project)
    write_readme(project / "README.md", items, manifest)
