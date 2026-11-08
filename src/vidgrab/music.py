"""音樂庫來源：Openverse（CC 授權，免 key）與 Jamendo（CC 授權，需免費 client id）。

做 Shorts 的背景音樂用。跟影片素材站一樣：搜尋走官方 API、結果帶直連檔案網址
（`Item.media_url`）、授權明確。差別是下載的是音訊檔。

授權策略（刻意保守，做影片要能商用、能混音）：
- 收：CC0、公共領域、CC BY（需標註）
- 不收：含 NC（禁商用）、ND（禁改作，混進影片就是改作）、SA（衍生作品要同授權，會牽連整支影片）
"""

from __future__ import annotations

import re
from typing import Any

import requests

from .models import Item, clean_text
from .sources import SourceError
from .stock import _CJK, UA, _key

OPENVERSE_API = "https://api.openverse.org/v1/audio/"
JAMENDO_API = "https://api.jamendo.com/v3.0/tracks/"

# Openverse 的授權代碼 → (license_kind, license_name, 人話)
_OK_LICENSES: dict[str, tuple[str, str, str]] = {
    "cc0": ("free", "CC0", "CC0：可自由使用"),
    "pdm": ("free", "Public Domain", "公共領域：可自由使用"),
    "by": ("free-attribution", "CC BY", "CC BY：可營利，需標註作者"),
}


def jamendo_key() -> str:
    """Jamendo client id（JAMENDO_CLIENT_ID / VIDGRAB_JAMENDO_KEY / auth/jamendo.key）。"""
    return _key(("JAMENDO_CLIENT_ID", "VIDGRAB_JAMENDO_KEY"), "jamendo.key")


def license_code_from_url(url: str) -> str:
    """從 creativecommons.org 的網址取授權代碼：.../licenses/by-nc-sa/3.0/ → by-nc-sa。"""
    url = (url or "").lower()
    m = re.search(r"/licenses/([a-z\-+]+)/", url)
    if m:
        return m.group(1)
    if "/publicdomain/zero/" in url:
        return "cc0"
    if "/publicdomain/mark/" in url:
        return "pdm"
    return ""


def classify(code: str, version: str = "") -> tuple[str, str, str] | None:
    """授權代碼 → (kind, name, label)；不能用於商用影片的回 None。"""
    info = _OK_LICENSES.get((code or "").lower())
    if not info:
        return None
    kind, name, label = info
    if version and code.lower() == "by":
        name = f"CC BY {version}"
    return kind, name, label


def _filter_common(item: Item, exclude: list[str] | None, min_seconds: float,
                   max_seconds: float) -> bool:
    if exclude and any(w.lower() in (item.title + " " + " ".join(item.tags)).lower()
                       for w in exclude):
        return False
    if min_seconds and (item.duration or 0) < min_seconds:
        return False
    return not (max_seconds and item.duration and item.duration > max_seconds)


class _MusicSource:
    name = ""
    timeout = 20.0

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA

    def _get(self, url: str, params: dict[str, Any]) -> Any:
        label = self.name.capitalize()
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            # 例外訊息會帶整串網址（Jamendo 的 client id 就在網址裡）：只報種類並切斷例外鏈
            raise SourceError(
                f"{label} 連線失敗（{type(exc).__name__}）：網路不通或被擋") from None
        if resp.status_code in (401, 403):
            raise SourceError(f"{label} 拒絕請求（{resp.status_code}）：確認 key／client id")
        if resp.status_code == 429:
            raise SourceError(f"{label} 限流（429）：稍後再試")
        if resp.status_code >= 500:
            raise SourceError(f"{label} 暫時不可用（HTTP {resp.status_code}）")
        try:
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"{label} 回應無法解析：{type(exc).__name__}") from None

    def detail(self, item: Item) -> None:
        """音樂庫沒有專門的詳情端點；授權在搜尋當下已經確認，這裡只標記已確認。"""
        item.checked = True


class OpenverseSource(_MusicSource):
    """Openverse 音訊搜尋。匿名可用（有較低的限流）。"""

    name = "openverse"

    def search(
        self,
        keyword: str,
        limit: int = 20,
        order: str = "relevance",
        duration: str = "any",
        exclude: list[str] | None = None,
        min_seconds: float = 0,
        max_seconds: float = 0,
        orientation: str = "",  # 音樂沒有方向；參數為了介面一致
    ) -> list[Item]:
        params: dict[str, Any] = {
            "q": keyword,
            "category": "music",
            "license_type": "commercial,modification",
            "page_size": max(1, min(20, limit * 2)),  # 匿名上限 20；還要自己再濾授權
        }
        data = self._get(OPENVERSE_API, params)
        items: list[Item] = []
        for raw in data.get("results") or []:
            item = self._to_item(raw, keyword)
            if item and _filter_common(item, exclude, min_seconds, max_seconds):
                items.append(item)
                if len(items) >= limit:
                    break
        return items

    def _to_item(self, raw: dict[str, Any], keyword: str) -> Item | None:
        vid = str(raw.get("id") or "")
        url = raw.get("url") or ""
        lic = classify(str(raw.get("license") or ""), str(raw.get("license_version") or ""))
        if not vid or not url or not lic:
            return None
        kind, lic_name, label = lic
        tags = [clean_text(str(t.get("name", ""))) for t in raw.get("tags") or []
                if isinstance(t, dict)]
        tags += [clean_text(str(g)) for g in raw.get("genres") or []]
        tags = [t for t in dict.fromkeys(tags) if t][:40]
        page = raw.get("foreign_landing_url") or url
        ms = raw.get("duration")
        item = Item(
            platform=self.name, vid=vid, url=page,
            title=clean_text(raw.get("title") or "") or f"Openverse audio {vid}",
            author=clean_text(raw.get("creator") or ""),
            duration=float(ms) / 1000.0 if ms else None,
            keyword=keyword,
            description=", ".join(tags),
            license_hint=lic_name,
        )
        item.media_url = url
        item.webpage_url = page
        item.author_url = raw.get("creator_url") or ""
        item.channel = item.author
        item.thumbnail = raw.get("thumbnail") or ""
        item.tags = tags
        item.license_kind, item.license_label, item.license_name = kind, label, lic_name
        item.description_full = clean_text(raw.get("attribution") or "")
        item.checked = True
        return item


class JamendoSource(_MusicSource):
    """Jamendo 音樂。需要免費的 client id（https://devportal.jamendo.com/）。"""

    name = "jamendo"

    def __init__(self, client_id: str | None = None) -> None:
        super().__init__()
        self.client_id = (client_id if client_id is not None else jamendo_key()).strip()
        if not self.client_id:
            raise SourceError(
                "沒有 Jamendo client id。到 https://devportal.jamendo.com/ 免費申請，"
                "設成環境變數 JAMENDO_CLIENT_ID（或寫進 auth/jamendo.key）")

    def search(
        self,
        keyword: str,
        limit: int = 20,
        order: str = "relevance",
        duration: str = "any",
        exclude: list[str] | None = None,
        min_seconds: float = 0,
        max_seconds: float = 0,
        orientation: str = "",
    ) -> list[Item]:
        params: dict[str, Any] = {
            "client_id": self.client_id,
            "format": "json",
            "limit": max(1, min(200, limit * 3)),  # 授權要自己濾（NC／ND／SA 很多）
            "search": keyword[:100],
            "audiodownload_allowed": "true",
            "include": "musicinfo",
            "order": "releasedate_desc" if order == "new" else "popularity_total_desc",
        }
        if _CJK.search(keyword):
            params["lang"] = "zh"
        data = self._get(JAMENDO_API, params)
        head = data.get("headers") or {}
        if head.get("status") not in (None, "success"):
            msg = clean_text(str(head.get("error_message") or head.get("status")))
            raise SourceError(f"Jamendo 回報錯誤：{msg}")
        items: list[Item] = []
        for raw in data.get("results") or []:
            item = self._to_item(raw, keyword)
            if item and _filter_common(item, exclude, min_seconds, max_seconds):
                items.append(item)
                if len(items) >= limit:
                    break
        return items

    def _to_item(self, raw: dict[str, Any], keyword: str) -> Item | None:
        vid = str(raw.get("id") or "")
        url = raw.get("audiodownload") or ""
        if not vid or not url or raw.get("audiodownload_allowed") is False:
            return None
        lic = classify(license_code_from_url(str(raw.get("license_ccurl") or "")),
                       _version_from_url(str(raw.get("license_ccurl") or "")))
        if not lic:
            return None
        kind, lic_name, label = lic
        tags_raw = ((raw.get("musicinfo") or {}).get("tags") or {})
        tags: list[str] = []
        for key in ("genres", "instruments", "vartags"):
            tags += [clean_text(str(t)) for t in tags_raw.get(key) or []]
        tags = [t for t in dict.fromkeys(tags) if t][:40]
        page = raw.get("shareurl") or f"https://www.jamendo.com/track/{vid}"
        artist = clean_text(raw.get("artist_name") or "")
        item = Item(
            platform=self.name, vid=vid, url=page,
            title=clean_text(raw.get("name") or "") or f"Jamendo track {vid}",
            author=artist,
            duration=float(raw["duration"]) if raw.get("duration") else None,
            keyword=keyword,
            description=", ".join(tags),
            license_hint=lic_name,
        )
        item.media_url = url
        item.webpage_url = page
        item.author_id = str(raw.get("artist_id") or "")
        if item.author_id:
            item.author_url = f"https://www.jamendo.com/artist/{item.author_id}"
        item.channel = artist
        item.thumbnail = raw.get("image") or ""
        item.tags = tags
        item.license_kind, item.license_label, item.license_name = kind, label, lic_name
        item.checked = True
        return item


def _version_from_url(url: str) -> str:
    m = re.search(r"/licenses/[a-z\-+]+/([0-9.]+)/", (url or "").lower())
    return m.group(1) if m else ""
