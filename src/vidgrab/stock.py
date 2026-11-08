"""素材站來源：Pexels、Pixabay（授權明確、可商用、有直連檔案）。

跟 B 站／YouTube 的差別：
- 搜尋走官方 API，要 API key（Pexels https://www.pexels.com/api/ 、
  Pixabay https://pixabay.com/api/docs/ ，都免費）
- 結果直接帶檔案網址（`Item.media_url`），下載不經 yt-dlp
- 授權固定是各站自己的授權：可商用、不必標註（標了更好）、不可原樣轉售
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


def _key(envs: tuple[str, ...], filename: str) -> str:
    """API key：環境變數優先，其次 auth 目錄下的檔案。"""
    for name in envs:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    try:
        from . import auth

        path: Path = auth.home() / filename
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def api_key() -> str:
    """Pexels API key（PEXELS_API_KEY / VIDGRAB_PEXELS_KEY / auth/pexels.key）。"""
    return _key(("PEXELS_API_KEY", "VIDGRAB_PEXELS_KEY"), "pexels.key")


def pixabay_key() -> str:
    """Pixabay API key（PIXABAY_API_KEY / VIDGRAB_PIXABAY_KEY / auth/pixabay.key）。"""
    return _key(("PIXABAY_API_KEY", "VIDGRAB_PIXABAY_KEY"), "pixabay.key")


ORIENTATIONS = ("any", "portrait", "landscape", "square")


def orientation_of(width: Any, height: Any) -> str:
    """portrait / landscape / square；尺寸不明回空字串。接近正方形（±5%）算 square。"""
    try:
        w, h = float(width), float(height)
    except (TypeError, ValueError):
        return ""
    if w <= 0 or h <= 0:
        return ""
    if abs(w - h) / max(w, h) <= 0.05:
        return "square"
    return "portrait" if h > w else "landscape"


def orientation_ok(item: Item, wanted: str) -> bool:
    """orientation 篩選：只對有尺寸的素材站影片生效；尺寸不明的保留。"""
    if not wanted or wanted == "any":
        return True
    actual = orientation_of(item.width, item.height)
    return not actual or actual == wanted


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
            params["orientation"] = orientation  # Pexels 原生支援
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
            if not orientation_ok(item, orientation):
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


# ---------------------------------------------------------------- Pixabay

PIXABAY_API = "https://pixabay.com/api/videos/"
PIXABAY_LICENSE = "Pixabay Content License"
PIXABAY_LICENSE_URL = "https://pixabay.com/service/license-summary/"
PIXABAY_LABEL = "Pixabay 授權：可商用、免標註（不可原樣轉售）"
# 檔案由大到小；large 有些影片沒有（url 為空字串）
_PIXABAY_SIZES = ("large", "medium", "small", "tiny")


def pick_pixabay_file(videos: dict[str, Any], max_height: int = 1080) -> dict[str, Any] | None:
    """從 large/medium/small/tiny 挑一個：短邊不超過 max_height 的最大者；都超過就取最小的。"""
    cands = []
    for name in _PIXABAY_SIZES:
        f = (videos or {}).get(name) or {}
        if f.get("url") and (f.get("width") or 0) > 0 and (f.get("height") or 0) > 0:
            cands.append(f)
    if not cands:
        return None

    def short(f: dict[str, Any]) -> int:
        return min(int(f["width"]), int(f["height"]))

    fit = [f for f in cands if short(f) <= max_height]
    return max(fit, key=short) if fit else min(cands, key=short)


class PixabaySource:
    """Pixabay 影片。API 沒有 orientation 參數，所以用檔案尺寸在本地篩。"""

    name = "pixabay"

    def __init__(self, key: str | None = None, max_height: int = 1080):
        self.key = (key if key is not None else pixabay_key()).strip()
        if not self.key:
            raise SourceError(
                "沒有 Pixabay API key。註冊後到 https://pixabay.com/api/docs/ 取得，"
                "設成環境變數 PIXABAY_API_KEY（或寫進 auth/pixabay.key）"
            )
        self.max_height = max_height
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self.timeout = 20.0

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self.session.get(
                PIXABAY_API, params={"key": self.key, **params}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise SourceError(f"Pixabay 連線失敗：{exc}") from exc
        if resp.status_code in (401, 403):
            raise SourceError("Pixabay 拒絕這把 API key（401/403）：確認 key 是否正確")
        if resp.status_code == 400:
            raise SourceError(f"Pixabay 拒絕這個查詢（400）：{(resp.text or '')[:120]}")
        if resp.status_code == 429:
            raise SourceError("Pixabay 限流（429）：每分鐘 100 次，稍後再試")
        if resp.status_code >= 500:
            raise SourceError(f"Pixabay 暫時不可用（HTTP {resp.status_code}）")
        try:
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"Pixabay 回應無法解析：{exc}") from exc

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
        # 本地還要篩方向，多要一些；API 的 per_page 範圍是 3–200
        want = limit * (3 if orientation and orientation != "any" else 2)
        params: dict[str, Any] = {
            "q": keyword[:100],  # API 上限 100 字元
            "per_page": max(3, min(200, want)),
            "video_type": "film",
            "safesearch": "true",
            "order": "latest" if order == "new" else "popular",
        }
        if _CJK.search(keyword):
            params["lang"] = "zh"
        data = self._get(params)
        items: list[Item] = []
        for raw in data.get("hits") or []:
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
            if not orientation_ok(item, orientation):
                continue
            items.append(item)
            if len(items) >= limit:
                break
        return items

    def _to_item(self, raw: dict[str, Any], keyword: str) -> Item | None:
        vid = str(raw.get("id") or "")
        chosen = pick_pixabay_file(raw.get("videos") or {}, self.max_height)
        if not vid or not chosen:
            return None
        tags = [t.strip() for t in str(raw.get("tags") or "").split(",") if t.strip()][:40]
        page = raw.get("pageURL") or f"https://pixabay.com/videos/id-{vid}/"
        title = ", ".join(tags[:4]) or f"Pixabay video {vid}"
        user = clean_text(raw.get("user") or "")
        user_id = str(raw.get("user_id") or "")
        item = Item(
            platform=self.name,
            vid=vid,
            url=page,
            title=title,
            author=user,
            duration=float(raw["duration"]) if raw.get("duration") else None,
            views=int(raw["views"]) if raw.get("views") is not None else None,
            keyword=keyword,
            description=", ".join(tags),
            license_hint=PIXABAY_LICENSE,
        )
        item.media_url = chosen["url"]
        item.webpage_url = page
        item.author_id = user_id
        if user and user_id:
            item.author_url = f"https://pixabay.com/users/{user}-{user_id}/"
        item.channel = user
        item.thumbnail = chosen.get("thumbnail") or ""
        item.width = int(chosen["width"])
        item.height = int(chosen["height"])
        item.filesize = int(chosen["size"]) if chosen.get("size") else None
        item.tags = tags
        item.license_kind = "free"
        item.license_label = PIXABAY_LABEL
        item.license_name = PIXABAY_LICENSE
        item.checked = True
        return item

    def detail(self, item: Item) -> None:
        """重新向 Pixabay 確認影片還在、並更新直連網址。"""
        if item.platform != self.name or not item.vid:
            return
        try:
            data = self._get({"id": item.vid})
        except SourceError as exc:
            item.error = f"詳情取得失敗：{exc}"
            return
        hits = data.get("hits") or []
        fresh = self._to_item(hits[0], item.keyword) if hits else None
        if fresh is None:
            item.error = "詳情取得失敗：影片不存在或沒有可用檔案"
            return
        item.media_url = fresh.media_url
        item.duration = fresh.duration or item.duration
        item.author, item.author_url = fresh.author or item.author, fresh.author_url or item.author_url
        item.thumbnail = fresh.thumbnail or item.thumbnail
        item.width, item.height = fresh.width, fresh.height
        item.license_kind, item.license_label = fresh.license_kind, fresh.license_label
        item.license_name = fresh.license_name
        item.error = ""
        item.checked = True


__all__ = [
    "PexelsSource", "PixabaySource", "api_key", "pixabay_key", "pick_file",
    "pick_pixabay_file", "orientation_of", "orientation_ok", "ORIENTATIONS",
    "PEXELS_LICENSE_URL", "PIXABAY_LICENSE_URL",
]
