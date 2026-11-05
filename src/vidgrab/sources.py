"""搜尋來源：B 站（原生 API，wbi 簽名）與 YouTube（yt-dlp）。"""

from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.parse
from typing import Any

import requests

from .models import Item, classify_license, clean_text, is_repost, log, matches_keyword

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 混淆表，B 站前端 wbi 簽名用；官方端點會不定期換，換了更新這張表即可
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

BILI_ORDERS = {"relevance": "totalrank", "views": "click", "new": "pubdate", "danmaku": "dm"}


class SourceError(RuntimeError):
    pass


class _Http:
    """帶重試、節流與 cookie 的極簡 HTTP 客戶端。"""

    def __init__(self, timeout: float = 20.0, retries: int = 3, delay: float = 0.6):
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
            }
        )

    def get(self, url: str, params: dict[str, Any] | None = None, **kw) -> requests.Response:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout, **kw)
                if resp.status_code in (412, 429, 503):
                    raise SourceError(f"HTTP {resp.status_code}（被限流）")
                resp.raise_for_status()
                time.sleep(self.delay + random.random() * self.delay)
                return resp
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(self.delay * (attempt + 2))
        raise SourceError(f"{url} 取得失敗：{last}")


# ---------------------------------------------------------------- B 站


class BilibiliSource:
    name = "bilibili"

    def __init__(self, http: _Http | None = None):
        self.http = http or _Http()
        self._mixinkey: str | None = None
        self._key_time = 0.0
        self._bootstrap()

    # -- 簽名 ---------------------------------------------------------

    def _bootstrap(self) -> None:
        try:
            spi = self.http.get("https://api.bilibili.com/x/frontend/finger/spi").json()
            data = spi.get("data") or {}
            if data.get("b_3"):
                self.http.session.cookies.set("buvid3", data["b_3"], domain=".bilibili.com")
            if data.get("b_4"):
                self.http.session.cookies.set("buvid4", data["b_4"], domain=".bilibili.com")
        except Exception:  # 沒有 buvid 也能試，只是比較容易被要求驗證
            pass
        # 有登入過就帶上 cookie：搜尋結果更完整，也比較不會被要求驗證
        try:
            from . import auth

            for name, value in auth.cookies_dict().items():
                self.http.session.cookies.set(name, value, domain=".bilibili.com")
        except Exception:  # noqa: BLE001
            pass
        # 先像真人一樣逛一下搜尋頁，拿到完整的 cookie 指紋；沒有這步常常只回空殼
        try:
            self.http.session.get(
                "https://search.bilibili.com/all", params={"keyword": "素材"}, timeout=15
            )
        except Exception:  # noqa: BLE001
            pass

    def _get_mixinkey(self, force: bool = False) -> str:
        if self._mixinkey and not force and time.time() - self._key_time < 3600:
            return self._mixinkey
        nav = self.http.get("https://api.bilibili.com/x/web-interface/nav").json()
        wbi = (nav.get("data") or {}).get("wbi_img") or {}
        try:
            img = wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
            sub = wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"拿不到 wbi 金鑰：{exc}") from exc
        raw = img + sub
        self._mixinkey = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        self._key_time = time.time()
        return self._mixinkey

    def _signed_params(self, params: dict[str, Any], force_key: bool = False) -> dict[str, Any]:
        params = dict(params)
        params["wts"] = int(time.time())
        query = urllib.parse.urlencode(sorted(params.items()))
        w_rid = hashlib.md5(
            (urllib.parse.quote(query, safe="") + self._get_mixinkey(force=force_key)).encode()
        ).hexdigest()
        params["w_rid"] = w_rid
        return params

    # -- 搜尋 ---------------------------------------------------------

    def search(
        self,
        keyword: str,
        limit: int = 20,
        order: str = "relevance",
        duration: str = "any",
        page_size: int = 20,
        exclude: list[str] | None = None,
        min_seconds: float = 0,
        max_seconds: float = 0,
    ) -> list[Item]:
        params_base: dict[str, Any] = {
            "search_type": "video",
            "keyword": keyword,
            "order": BILI_ORDERS.get(order, "totalrank"),
            "page": 1,
            "page_size": page_size,
            "tids": 0,
        }
        if duration != "any":
            params_base["duration"] = {"short": 1, "medium": 2, "long": 3, "verylong": 4}.get(duration, 0)

        items: list[Item] = []
        page = 1
        retried = False
        while len(items) < limit and page <= 10:
            payload = self._search_page(params_base, page)
            if payload.get("code") != 0:
                if page == 1:
                    raise SourceError(f"B 站搜尋被擋：{payload.get('code')} {payload.get('message')}")
                break
            data = payload.get("data") or {}
            results = data.get("result") or []
            # 只回 v_voucher = 被要求驗證，換一組 cookie 再試一次
            if not results and data.get("v_voucher") and not retried:
                retried = True
                # v_voucher 是風控（短時間搜太多次最常見），不是 cookie 壞掉。
                # 原本寫「換 cookie」會把使用者帶去重掃碼，白忙一場。
                log("· B 站風控攔住了（短時間搜太多次），冷卻後再試；"
                    "換 cookie 通常沒用，除非真的掉登入。")
                self._bootstrap()
                time.sleep(1.5)
                payload = self._search_page(params_base, page, force_key=True)
                results = (payload.get("data") or {}).get("result") or []
            if not results:
                break
            for raw in results:
                if raw.get("type") != "video":
                    continue
                item = self._to_item(raw, keyword)
                if exclude and any(word.lower() in item.title.lower() for word in exclude):
                    continue
                if not matches_keyword(item, keyword):
                    continue
                if not _length_ok(item.duration, min_seconds, max_seconds):
                    continue
                items.append(item)
                if len(items) >= limit:
                    break
            page += 1
        return items

    def _search_page(self, params_base: dict[str, Any], page: int, force_key: bool = False) -> dict[str, Any]:
        params = self._signed_params({**params_base, "page": page}, force_key=force_key)
        resp = self.http.session.get(
            "https://api.bilibili.com/x/web-interface/wbi/search/type",
            params=params,
            timeout=self.http.timeout,
            headers={"Referer": f"https://search.bilibili.com/all?keyword={urllib.parse.quote(params_base['keyword'])}"},
        )
        time.sleep(self.http.delay)
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {"code": -1, "message": resp.status_code}

    def _to_item(self, raw: dict[str, Any], keyword: str) -> Item:
        bvid = raw.get("bvid") or ""
        desc = clean_text(raw.get("description") or "")
        title = clean_text(raw.get("title") or "")
        item = Item(
            platform=self.name,
            vid=bvid or str(raw.get("aid") or ""),
            url=f"https://www.bilibili.com/video/{bvid}" if bvid else raw.get("arcurl", ""),
            title=title,
            author=clean_text(raw.get("author") or ""),
            duration=_bili_duration(raw.get("duration")),
            views=_int(raw.get("play")),
            pubdate=time.strftime("%Y%m%d", time.localtime(raw["pubdate"])) if raw.get("pubdate") else "",
            keyword=keyword,
            description=desc,
            license_hint=clean_text(raw.get("tag") or ""),
        )
        # B 站搜尋結果的 tag 是逗號分隔字串，拆成清單給素材庫用
        tag_raw = clean_text(raw.get("tag") or "")
        if tag_raw:
            item.tags = [t.strip() for t in tag_raw.replace("，", ",").split(",") if t.strip()][:40]
        if raw.get("pic"):
            pic = str(raw["pic"])
            item.thumbnail = pic if pic.startswith("http") else f"https:{pic}"
        item.license_kind, item.license_label = classify_license(item.blob())
        item.repost = is_repost(item.blob())
        return item

    def _tags(self, bvid: str) -> list[str]:
        """取 B 站影片標籤。取不到就回空清單，不讓整支詳情失敗。"""
        try:
            resp = self.http.get(
                "https://api.bilibili.com/x/tag/archive/tags", params={"bvid": bvid}
            )
            payload = resp.json()
            if payload.get("code") != 0:
                return []
            names: list[str] = []
            for t in payload.get("data") or []:
                name = (t or {}).get("tag_name")
                if name and name not in names:
                    names.append(str(name))
            return names[:40]
        except Exception:  # noqa: BLE001
            return []

    # -- 單支影片詳情（含「原創／轉載」欄位） ---------------------------

    def detail(self, item: Item) -> None:
        if item.platform != self.name or not item.vid:
            return
        try:
            resp = self.http.get(
                "https://api.bilibili.com/x/web-interface/view", params={"bvid": item.vid}
            )
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            item.error = f"詳情取得失敗：{exc}"
            return
        data = payload.get("data") or {}
        if payload.get("code") != 0 or not data:
            item.error = f"詳情取得失敗：{payload.get('message') or payload.get('code')}"
            return
        item.title = data.get("title") or item.title
        item.description = data.get("desc") or item.description
        item.duration = data.get("duration") or item.duration
        item.views = (data.get("stat") or {}).get("view", item.views)
        owner = data.get("owner") or {}
        item.author = owner.get("name", item.author)
        # B 站是自己的一套欄位，這裡逐一對應成素材庫要的樣子
        if owner.get("mid"):
            item.author_id = item.author_id or str(owner["mid"])
            item.author_url = item.author_url or f"https://space.bilibili.com/{owner['mid']}"
        item.channel = item.channel or item.author
        item.webpage_url = item.webpage_url or f"https://www.bilibili.com/video/{item.vid}"
        if data.get("pic"):
            pic = str(data["pic"])
            item.thumbnail = item.thumbnail or (pic if pic.startswith("http") else f"https:{pic}")
        if data.get("tname"):
            item.categories = item.categories or [str(data["tname"])]
        # 標籤：/x/web-interface/view 不含 tags，要另外問一支 API
        if not item.tags:
            item.tags = self._tags(item.vid)
        if data.get("desc") and not item.description_full:
            item.description_full = str(data["desc"])
        if data.get("pubdate"):
            item.pubdate = time.strftime("%Y%m%d", time.localtime(data["pubdate"]))
            item.upload_date = time.strftime("%Y-%m-%d", time.localtime(data["pubdate"]))
        # copyright: 1 = 原創, 2 = 轉載
        if data.get("copyright") == 2:
            item.repost = True
            item.check_note = "B 站標記為「轉載」，非原作者上傳"
        item.license_kind, item.license_label = classify_license(item.blob())
        item.checked = True


def _bili_duration(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- YouTube


class YoutubeSource:
    name = "youtube"

    def __init__(self):
        try:
            import yt_dlp  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise SourceError("需要 yt-dlp：pip install yt-dlp") from exc

    def _ydl(self, **overrides):
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noprogress": True,
            "retries": 2,
            "socket_timeout": 20,
            "ignoreerrors": True,
            "extractor_args": {"youtube": {"skip": ["hls", "dash"]}},
        }
        opts.update(overrides)
        return yt_dlp.YoutubeDL(opts)

    def search(
        self,
        keyword: str,
        limit: int = 20,
        order: str = "relevance",
        duration: str = "any",
        exclude: list[str] | None = None,
        min_seconds: float = 0,
        max_seconds: float = 0,
    ) -> list[Item]:
        fetch = max(limit * 3, limit + 8)
        with self._ydl(extract_flat="in_playlist") as ydl:
            info = ydl.extract_info(f"ytsearch{fetch}:{keyword}", download=False)
        entries = [e for e in ((info or {}).get("entries") or []) if e]
        items: list[Item] = []
        for raw in entries:
            title = clean_text(raw.get("title") or "")
            if exclude and any(word.lower() in title.lower() for word in exclude):
                continue
            secs = raw.get("duration")
            if duration != "any" and not _duration_match(secs, duration):
                continue
            desc = clean_text(raw.get("description") or "")
            item = Item(
                platform=self.name,
                vid=raw.get("id") or "",
                url=raw.get("url") or f"https://www.youtube.com/watch?v={raw.get('id')}",
                title=title,
                author=clean_text(raw.get("channel") or raw.get("uploader") or ""),
                duration=float(secs) if secs else None,
                views=_int(raw.get("view_count")),
                pubdate=time.strftime("%Y%m%d", time.localtime(raw["timestamp"])) if raw.get("timestamp") else "",
                keyword=keyword,
                description=desc,
            )
            from .models import enrich_from_info

            enrich_from_info(item, raw)
            item.license_kind, item.license_label = classify_license(item.blob())
            item.repost = is_repost(item.blob())
            if not matches_keyword(item, keyword):
                continue
            if not _length_ok(item.duration, min_seconds, max_seconds):
                continue
            items.append(item)
            if len(items) >= limit:
                break
        if order == "views":
            items.sort(key=lambda i: i.views or 0, reverse=True)
        elif order == "new":
            items.sort(key=lambda i: i.pubdate or "", reverse=True)
        return items

    def detail(self, item: Item) -> None:
        """完整抓一次，主要為了 YouTube 官方授權欄位（CC BY 或標準授權）。"""
        try:
            with self._ydl(extract_flat=False) as ydl:
                info = ydl.extract_info(item.url, download=False)
        except Exception as exc:  # noqa: BLE001
            item.error = f"詳情取得失敗：{exc}"
            return
        if not info:
            item.error = "詳情取得失敗（沒有回傳內容）"
            return
        item.title = clean_text(info.get("title") or item.title)
        item.description = clean_text(info.get("description") or item.description)[:2000]
        item.duration = info.get("duration") or item.duration
        item.views = info.get("view_count") or item.views
        item.author = clean_text(info.get("channel") or info.get("uploader") or item.author)
        if info.get("upload_date"):
            item.pubdate = info["upload_date"]
        # 一次把標籤、作者頁、解析度等素材庫要用的欄位補齊
        from .models import enrich_from_info

        enrich_from_info(item, info)
        lic = (info.get("license") or "").strip()
        item.channel_license = lic
        if lic:
            if "creative commons" in lic.lower():
                item.license_kind = "free-attribution"
                item.license_label = "YouTube 標示 CC BY（可營利，需標註）"
            elif "standard youtube" in lic.lower():
                item.license_kind = "unknown"
                item.license_label = "YouTube 標準授權（不可直接挪用）"
        if not item.license_label:
            item.license_kind, item.license_label = classify_license(item.blob())
        item.checked = True


def _length_ok(seconds: Any, min_seconds: float, max_seconds: float) -> bool:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return True  # 長度不明就留著，後面切片時再判斷
    if min_seconds and seconds < min_seconds:
        return False
    if max_seconds and seconds > max_seconds:
        return False
    return True


def _duration_match(seconds: Any, bucket: str) -> bool:
    if not isinstance(seconds, (int, float)):
        return True
    if bucket == "short":
        return seconds < 600
    if bucket == "medium":
        return 600 <= seconds < 1800
    if bucket == "long":
        return 1800 <= seconds < 3600
    if bucket == "verylong":
        return seconds >= 3600
    return True


def get_source(platform: str, http: _Http | None = None):
    if platform in ("bilibili", "bili", "b"):
        return BilibiliSource(http)
    if platform in ("youtube", "yt", "y"):
        return YoutubeSource()
    if platform in ("both", "all"):
        raise ValueError("both 請用 get_sources()")
    raise ValueError(f"不認識的平台：{platform}")


def get_sources(platform: str, http: _Http | None = None) -> list:
    if platform in ("both", "all"):
        out = []
        for name in ("bilibili", "youtube"):
            try:
                out.append(get_source(name, http))
            except SourceError as exc:
                log(f"! {name} 不可用：{exc}")
        return out
    return [get_source(platform, http)]


def pretty_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
