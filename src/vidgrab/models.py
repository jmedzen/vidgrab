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

    # ---- 素材庫需要的資訊（下載時就寫好，不必回頭爬） ----
    tags: list[str] = field(default_factory=list)          # 標籤／分類
    categories: list[str] = field(default_factory=list)     # 平台分類
    author_url: str = ""                                    # 作者頁面（要聯繫／標註用）
    author_id: str = ""                                     # 作者 uid / channel id
    channel: str = ""                                       # 頻道名
    webpage_url: str = ""                                   # 原始頁面
    thumbnail: str = ""                                     # 封面
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    filesize: int | None = None
    language: str = ""
    license_name: str = ""                                  # yt-dlp 的 license 欄位
    description_full: str = ""                              # 完整說明（不受截斷）
    info_json: str = ""                                     # sidecar json 路徑

    def asset_id(self) -> str:
        """agent 用來引用這支影片的穩定鍵。"""
        return asset_id(self.platform, self.vid)

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


# ---------------------------------------------------------------- 素材 metadata


# yt-dlp 會把一些「技術狀態」也塞進 tags，例如 yt:cc=on（有字幕）、
# yt:stretch=16:9。這些不是內容關鍵字，進素材庫只會變成雜訊。
_TECH_TAG = re.compile(r"^(yt:|yt-dlp:|language:|quality:)", re.IGNORECASE)


def _is_tech_tag(tag: str) -> bool:
    return bool(_TECH_TAG.match(tag.strip()))


def enrich_from_info(item: "Item", info: dict[str, Any]) -> "Item":
    """把 yt-dlp 的 info dict 補進 Item。

    設計原則：**下載當下就寫好**，因為影片之後可能下架、改標題、改授權。
    這裡只做「有就拿」，拿不到就留空，不猜、不編造。
    """
    info = info or {}

    def _s(key: str) -> str:
        v = info.get(key)
        return str(v).strip() if v not in (None, "") else ""

    def _tags(key: str) -> list[str]:
        raw = info.get(key) or []
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",")]
        out: list[str] = []
        for t in raw:
            t = str(t).strip()
            if not t or _is_tech_tag(t):
                continue
            if t not in out:
                out.append(t)
        return out[:40]

    if not item.author:
        item.author = _s("uploader") or _s("channel") or _s("creator")
    item.channel = item.channel or _s("channel") or _s("uploader")
    item.author_id = item.author_id or _s("channel_id") or _s("uploader_id")
    item.author_url = item.author_url or _s("channel_url") or _s("uploader_url")
    item.webpage_url = item.webpage_url or _s("webpage_url") or item.url
    item.thumbnail = item.thumbnail or _s("thumbnail")
    item.language = item.language or _s("language")

    if not item.tags:
        item.tags = _tags("tags")
    if not item.categories:
        item.categories = _tags("categories")

    if not item.description_full:
        item.description_full = _s("description")
    if not item.description:
        item.description = (item.description_full or "")[:2000]

    if not item.pubdate:
        raw = info.get("upload_date") or ""
        if raw and len(str(raw)) == 8:
            item.pubdate = str(raw)
        elif info.get("timestamp"):
            try:
                item.pubdate = time.strftime("%Y%m%d", time.localtime(float(info["timestamp"])))
            except Exception:  # noqa: BLE001
                pass
    if not item.upload_date:
        item.upload_date = _s("upload_date")

    if item.width is None and info.get("width"):
        try:
            item.width = int(info["width"])
        except Exception:  # noqa: BLE001
            pass
    if item.height is None and info.get("height"):
        try:
            item.height = int(info["height"])
        except Exception:  # noqa: BLE001
            pass
    if item.fps is None and info.get("fps"):
        try:
            item.fps = float(info["fps"])
        except Exception:  # noqa: BLE001
            pass

    if item.filesize is None:
        for key in ("filesize", "filesize_approx"):
            if info.get(key):
                try:
                    item.filesize = int(info[key])
                    break
                except Exception:  # noqa: BLE001
                    pass

    item.license_name = item.license_name or _s("license")

    if not item.views and info.get("view_count"):
        try:
            item.views = int(info["view_count"])
        except Exception:  # noqa: BLE001
            pass

    if not item.vid:
        item.vid = _s("id")

    return item


def search_terms(item: "Item") -> list[str]:
    """可以用來搜尋／比對這支素材的關鍵字。

    由「標籤 + 分類 + 使用者的搜尋詞」組成，去重後保留順序。
    這是素材庫最實用的一欄：想找「廚房 特寫」時，靠的就是這些詞。
    """
    out: list[str] = []
    for group in (item.tags, item.categories, (item.keyword or "").split()):
        for t in group:
            t = str(t).strip()
            if not t or _is_tech_tag(t):
                continue
            if t.lower() not in {x.lower() for x in out}:
                out.append(t)
    return out[:60]


def contact_block(item: "Item") -> dict[str, str]:
    """要標註出處／聯繫作者時需要的資訊。

    刻意只放「公開頁面」的連結，不放任何爬到的私人聯絡方式。
    """
    return {
        "author": item.author,
        "channel": item.channel,
        "author_url": item.author_url,
        "webpage_url": item.webpage_url or item.url,
        "license": item.license_name or item.license_label,
        "credit_hint": _credit_hint(item),
    }


def _credit_hint(item: "Item") -> str:
    """給剪輯者看的「這段要怎麼標註」一句話。"""
    who = item.author or item.channel or "原作者"
    where = item.webpage_url or item.url
    if item.license_name:
        return f"{who}（{item.license_name}）— {where}"
    if item.license_kind in ("free", "free-attribution"):
        return f"{who} — {where}"
    return f"授權未標示，使用前請先聯繫 {who}：{where}"


# ---------------------------------------------------------------- 穩定 ID

def asset_id(platform: str, vid: str) -> str:
    """一支影片的穩定 id。

    刻意用「平台 + 平台自己的 id」，不用資料庫序號：
    重新掃描、搬專案、重建清單都不會變，agent 可以長期引用。
    """
    return f"{platform}:{vid}" if vid else ""


def clip_id(platform: str, vid: str, start: float | None, end: float | None) -> str:
    """一個片段的穩定 id：影片 id + 秒數範圍。

    同一個範圍重下載會得到同一個 id（因為檔名也是這樣定的），
    所以 agent 可以用它做「這個抓過了嗎」的判斷。
    """
    base = asset_id(platform, vid)
    if not base:
        return ""
    if start is None:
        return f"{base}#full"
    return f"{base}#{_ts_key(start)}-{_ts_key(end)}"


def _ts_key(seconds: float | None) -> str:
    """秒數轉成固定寬度的鍵，避免 10 與 10.0 變成兩個 id。"""
    if seconds is None:
        return "0"
    return f"{float(seconds):.3f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- 結構化錯誤

class VidgrabError(Exception):
    """所有可預期錯誤的基底。帶機器可讀的 code 與可重試性。"""

    code = "error"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None,
                 retryable: bool | None = None, hint: str = ""):
        super().__init__(message)
        if code:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        out = {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }
        if self.hint:
            out["hint"] = self.hint
        return out


class NotFound(VidgrabError):
    code = "not_found"


class BadInput(VidgrabError):
    code = "bad_input"


class NoProject(VidgrabError):
    code = "no_project"
    hint = "先跑一次 search 建立專案"


class DownloadFailed(VidgrabError):
    code = "download_failed"
    retryable = True
    hint = "可能是網路或影片暫時不可用，稍後重試"


class LoginRequired(VidgrabError):
    code = "login_required"
    hint = "跑 ./vidgrab login 掃碼登入 B 站（高畫質／會員片需要）"


class MissingFFmpeg(VidgrabError):
    code = "missing_ffmpeg"
    hint = "brew install ffmpeg"


# ---------------------------------------------------------------- 內容類型

# 這些字出現在標題裡，通常代表「教學／課程／解說」，不是可直接剪的畫面素材。
# 刻意保守：只收幾乎不可能出現在實拍素材標題裡的詞。
_TUTORIAL_TITLE = re.compile(
    r"(教程|教学|課程|课程|公开课|公開課|全集|第[\d一二三四五六七八九十百]+[讲讲講課课]|"
    r"零基础|零基礎|入门|入門|从入门到|從入門到|"
    r"讲解|講解|解析|测评|評測|开箱|開箱|"
    r"建模|渲染|插画|插畫|MC动画|\bVlog\b|"
    r"直播回放|直播录像|直播錄像)",
    re.IGNORECASE,
)

# 明顯是長篇講課／節目的標題特徵
_TUTORIAL_DURATION_HINT = 3600.0  # 超過一小時且標題像課程，就當課程


def looks_like_tutorial(item: "Item") -> bool:
    """這支是不是「教學／課程」而非可剪的素材畫面？

    用標題判斷，因為 B 站的分區（tname）太粗，
    而「公開課」「建模教學」這類字幾乎不出現在空鏡素材上。
    """
    title = item.title or ""
    if _TUTORIAL_TITLE.search(title):
        return True
    # 標題含「講」「课」又特別長，多半是整堂課
    if (item.duration or 0) > _TUTORIAL_DURATION_HINT and re.search(r"[講讲課课]", title):
        return True
    return False
