"""共用的資料結構與小工具。"""

from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------- 授權分類


LICENSE_RULES: list[tuple[str, str, str]] = [
    # (判斷用的關鍵字, 類別, 人話說明)
    ("cc0", "free", "CC0 / 公共領域"),
    ("public domain", "free", "公共領域"),
    ("公眾領域", "free", "公共領域"),
    ("cc-by", "free-attribution", "CC BY：可營利，需標註作者"),
    ("creative commons attribution", "free-attribution", "CC BY：可營利，需標註作者"),
    ("知識共享", "check", "聲稱 CC，需點進去看是哪一種"),
    ("cc by", "free-attribution", "CC BY：可營利，需標註作者"),
    ("stock footage", "check", "素材站影片，多數要授權，需確認"),
    ("free to use", "check", "自稱可免費使用，來源需確認"),
    ("royalty free", "check", "免權利金，但通常仍有限制條款"),
    ("free download", "check", "自稱可免費下載，來源需確認"),
    ("no copyright", "check", "自稱無版權，需確認原始來源"),
    ("copyright free", "check", "自稱無版權，需確認原始來源"),
]

LICENSE_LABELS = {
    "free": "可自由使用",
    "free-attribution": "可用，需標註",
    "check": "需確認授權",
    "unknown": "未知／預設不可用",
    "none": "無授權資訊",
}

# B 站「轉載」= 非原作者上傳，通常代表原出處另有其人
REPOST_MARKERS = ("转载", "轉載", "搬运", "搬運", "侵删", "侵刪", "非原创", "非原創")


def classify_license(blob: str) -> tuple[str, str]:
    """從標題／說明文字猜授權等級。回傳 (類別, 說明)。"""
    text = (blob or "").lower()
    for needle, kind, label in LICENSE_RULES:
        if needle in text:
            return kind, label
    return "unknown", "未標示授權（預設當作不可直接用）"


def is_repost(blob: str) -> bool:
    text = blob or ""
    return any(marker in text for marker in REPOST_MARKERS)


# ---------------------------------------------------------------- 時間格式

_TS_PATTERNS = [
    re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)$"),  # [h:]mm:ss[.ms]
    re.compile(r"^(\d{1,2}):(\d{2}(?:\.\d+)?)$"),  # mm:ss
    re.compile(r"^(\d+(?:\.\d+)?)s?$"),  # 12 / 12s / 12.5
]


def parse_ts(value: str) -> float:
    """把 83、1:23、00:01:23.5 這類寫法轉成秒。"""
    text = str(value).strip()
    for pattern in _TS_PATTERNS:
        m = pattern.match(text)
        if not m:
            continue
        groups = m.groups()
        if len(groups) == 3 and pattern is _TS_PATTERNS[0]:
            hours, minutes, seconds = groups
            return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)
        if len(groups) == 2 and pattern is _TS_PATTERNS[1]:
            return int(groups[0]) * 60 + float(groups[1])
        if len(groups) == 1:
            return float(groups[0])
    raise ValueError(f"看不懂的時間格式：{value}（可用 83、1:23、00:01:23）")


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    return fmt_ts(seconds)


# ---------------------------------------------------------------- 文字處理


def clean_text(text: str) -> str:
    """去掉 B 站搜尋結果裡的 <em class="keyword"> 標記與多餘空白。"""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def width(text: str) -> int:
    """粗略計算顯示寬度，中文算 2。"""
    total = 0
    for ch in text:
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def pad(text: str, size: int) -> str:
    text = text or ""
    w = width(text)
    if w > size:
        out, acc = "", 0
        for ch in text:
            step = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if acc + step > size - 1:
                break
            out += ch
            acc += step
        return out + "…" + " " * max(0, size - acc - 1)
    return text + " " * (size - w)


def slugify(text: str, limit: int = 40) -> str:
    text = unicodedata.normalize("NFKC", text or "").strip()
    text = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "-", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._-")
    return (text or "clip")[:limit]


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f}{unit}" if unit == "B" else f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}GB"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 資料結構


@dataclass
class Item:
    """一支候選影片。"""

    platform: str  # bilibili / youtube
    vid: str  # BV 號或 YouTube video id
    url: str
    title: str
    author: str = ""
    duration: float | None = None
    views: int | None = None
    pubdate: str = ""  # YYYYMMDD
    keyword: str = ""
    description: str = ""
    upload_date: str = ""
    license_hint: str = ""

    # 執行期補上
    license_kind: str = "unknown"
    license_label: str = ""
    repost: bool = False
    channel_license: str = ""  # YouTube 頁面回報的授權欄位
    checked: bool = False
    check_note: str = ""
    error: str = ""
    clips: list[dict[str, Any]] = field(default_factory=list)
    thumbs: list[str] = field(default_factory=list)

    def blob(self) -> str:
        return " ".join([self.title, self.description, self.license_hint])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def today() -> str:
    return time.strftime("%Y%m%d")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def keyword_tokens(keyword: str) -> list[str]:
    """把關鍵字拆成比對用的詞：空白切開，太短的丟掉（除非是英文）。"""
    raw = [t.strip().lower() for t in re.split(r"[\s/|,，、]+", keyword or "") if t.strip()]
    return [t for t in raw if len(t) >= 2 or t.isascii()]


def matches_keyword(item: "Item", keyword: str) -> bool:
    """標題或說明至少命中一個關鍵字詞，才算是真的搜到（B 站常混進不相干的歌）。"""
    tokens = keyword_tokens(keyword)
    if not tokens:
        return True
    haystack = f"{item.title} {item.description}".lower()
    return any(token in haystack for token in tokens)


def iter_unique(items: Iterable[Item]) -> list[Item]:
    seen: set[tuple[str, str]] = set()
    out: list[Item] = []
    for item in items:
        key = (item.platform, item.vid)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
