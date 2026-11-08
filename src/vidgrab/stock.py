"""素材站來源：Pexels（授權明確、可商用、有直連檔案）。

跟 B 站／YouTube 的差別：
- 搜尋走官方 API，要 API key（免費申請：https://www.pexels.com/api/）
- 結果直接帶檔案網址（`Item.media_url`），下載不經 yt-dlp
- 授權固定是 Pexels License：可商用、不必標註（標了更好）、不可原樣轉售
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import requests

from .models import Item, clean_text
from .sources import SourceError

API = "https://api.pexels.com/videos"
UA = "vidgrab (+https://github.com/jmedzen/vidgrab)"
PEXELS_LICENSE = "Pexels License"
PEXELS_LICENSE_URL = "https://www.pexels.com/license/"
PEXELS_LABEL = "Pexels 授權：可商用、免標註（不可原樣轉售）"

_CJK = re.compile(r"[㐀-鿿]")


def api_key() -> str:
    """Pexels API key。環境變數優先，其次 auth 目錄下的 pexels.key。"""
    for name in ("PEXELS_API_KEY", "VIDGRAB_PEXELS_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    try:
        from . import auth

        path: Path = auth.home() / "pexels.key"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _title_from_url(url: str, fallback: str) -> str:
    """Pexels API 不回標題，只有網址 slug：/video/aerial-view-of-a-city-123456/。"""
    m = re.search(r"/video/([^/?#]+?)(?:-\d+)?/?(?:[?#].*)?$", url or "")
    if not m:
        return fallback
    words = m.group(1).replace("-", " ").strip()
    return (words[:1].upper() + words[1:]) if words else fallback


def pick_file(files: list[dict[str, Any]], max_height: int = 1080) -> dict[str, Any] | None:
    """挑一個 mp4 檔：短邊不超過 max_height 的最大者；都超過就取最小的。

    用短邊比：直式 1080x1920 與橫式 1920x1080 都算 1080。
    """
    cands = [
        f for f in files or []
        if f.get("link") and (f.get("file_type") or "video/mp4") == "video/mp4"
        and (f.get("width") or 0) > 0 and (f.get("height") or 0) > 0
    ]
    if not cands:
        return None

    def short(f: dict[str, Any]) -> int:
        return min(int(f["width"]), int(f["height"]))

    fit = [f for f in cands if short(f) <= max_height]
    if fit:
        return max(fit, key=lambda f: (short(f), f.get("fps") or 0))
    return min(cands, key=short)


class PexelsSource:
    name = "pexels"

    def __init__(self, key: str | None = None, max_height: int = 1080):
        self.key = (key if key is not None else api_key()).strip()
        if not self.key:
            raise SourceError(
                "沒有 Pexels API key。到 https://www.pexels.com/api/ 免費申請，"
                "設成環境變數 PEXELS_API_KEY（或寫進 auth/pexels.key）"
            )
        self.max_height = max_height
        self.session = requests.Session()
        self.session.headers.update({"Authorization": self.key, "User-Agent": UA})
        self.timeout = 20.0

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            resp = self.session.get(f"{API}{path}", params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SourceError(f"Pexels 連線失敗：{exc}") from exc
        if resp.status_code in (401, 403):
            raise SourceError("Pexels 拒絕這把 API key（401/403）：確認 key 是否正確")
        if resp.status_code == 429:
            raise SourceError("Pexels 限流（429）：每小時 200 次、每月 20000 次，稍後再試")
        if resp.status_code >= 500:
            raise SourceError(f"Pexels 暫時不可用（HTTP {resp.status_code}）")
        if resp.status_code == 404:
            raise SourceError("Pexels 找不到這支影片（可能已下架）")
        try:
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"Pexels 回應無法解析：{exc}") from exc

    def search(
        self,
        keyword: str,
        limit: int = 20,
        order: str = "relevance",  # Pexels 只有相關度排序；參數為了介面一致
        duration: str = "any",
        exclude: list[str] | None = None,
        min_seconds: float = 0,
        max_seconds: float = 0,
        orientation: str = "",
    ) -> list[Item]:
        params: dict[str, Any] = {"query": keyword, "per_page": max(1, min(80, limit * 2))}
        if orientation in ("portrait", "landscape", "square"):
            params["orientation"] = orientation
        if _CJK.search(keyword):
            params["locale"] = "zh-TW"
        data = self._get("/search", params)
        items: list[Item] = []
        for raw in data.get("videos") or []:
            item = self._to_item(raw, keyword)
            if item is None:
                continue
            if exclude and any(w.lower() in (item.title + " " + " ".join(item.tags)).lower()
                               for w in exclude):
                continue
            if min_seconds and (item.duration or 0) < min_seconds:
                continue
            if max_seconds and item.duration and item.duration > max_seconds:
                continue
            items.append(item)
            if len(items) >= limit:
                break
        return items

    def _to_item(self, raw: dict[str, Any], keyword: str) -> Item | None:
        vid = str(raw.get("id") or "")
        chosen = pick_file(raw.get("video_files") or [], self.max_height)
        if not vid or not chosen:
            return None
        page = raw.get("url") or f"https://www.pexels.com/video/{vid}/"
        user = raw.get("user") or {}
        title = _title_from_url(page, f"Pexels video {vid}")
        item = Item(
            platform=self.name,
            vid=vid,
            url=page,
            title=title,
            author=clean_text(user.get("name") or ""),
            duration=float(raw["duration"]) if raw.get("duration") else None,
            keyword=keyword,
            description=title,
            license_hint=PEXELS_LICENSE,
        )
        item.media_url = chosen["link"]
        item.webpage_url = page
        item.author_url = user.get("url") or ""
        item.author_id = str(user.get("id") or "")
        item.channel = item.author
        item.thumbnail = raw.get("image") or ""
        item.width = int(chosen["width"])
        item.height = int(chosen["height"])
        if chosen.get("fps"):
            item.fps = float(chosen["fps"])
        item.tags = [t for t in (clean_text(str(x)) for x in (raw.get("tags") or [])) if t][:40]
        item.license_kind = "free"
        item.license_label = PEXELS_LABEL
        item.license_name = PEXELS_LICENSE
        item.checked = True
        return item

    def detail(self, item: Item) -> None:
        """重新向 Pexels 確認影片還在、並更新直連網址（舊網址可能失效）。"""
        if item.platform != self.name or not item.vid:
            return
        try:
            raw = self._get(f"/videos/{item.vid}")
        except SourceError as exc:
            item.error = f"詳情取得失敗：{exc}"
            return
        fresh = self._to_item(raw, item.keyword)
        if fresh is None:
            item.error = "詳情取得失敗：沒有可用的檔案"
            return
        item.media_url = fresh.media_url
        item.duration = fresh.duration or item.duration
        item.author = fresh.author or item.author
        item.author_url = fresh.author_url or item.author_url
        item.thumbnail = fresh.thumbnail or item.thumbnail
        item.width, item.height, item.fps = fresh.width, fresh.height, fresh.fps
        item.license_kind, item.license_label = fresh.license_kind, fresh.license_label
        item.license_name = fresh.license_name
        item.error = ""
        item.checked = True


__all__ = ["PexelsSource", "api_key", "pick_file", "PEXELS_LICENSE_URL"]
