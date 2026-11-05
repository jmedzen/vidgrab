"""B 站掃碼登入：產 QR → 輪詢 → 把 cookie 存成 yt-dlp 認得的格式。"""

from __future__ import annotations

import http.cookiejar
import json
import time
from pathlib import Path
from typing import Any

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

STATUS = {
    0: ("ok", "登入成功"),
    86101: ("waiting", "還沒掃碼"),
    86090: ("scanned", "已掃碼，等你在手機上按確認"),
    86038: ("expired", "QR 過期了，重新產生一次"),
}

COOKIE_FIELDS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid", "buvid3", "buvid4")


def home() -> Path:
    import os

    root = Path(os.environ.get("VIDGRAB_HOME") or Path.cwd() / "projects").parent
    path = root / "auth"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cookie_file() -> Path:
    return home() / "bilibili-cookies.txt"


def state_file() -> Path:
    return home() / "bilibili.json"


def generate() -> dict[str, Any]:
    resp = requests.get(
        GEN,
        params={"source": "main-fe-header"},
        headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
        timeout=20,
    )
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"B 站不給 QR：{payload}")
    data = payload["data"]
    return {"key": data["qrcode_key"], "url": data["url"], "png": qr_png(data["url"])}


def qr_png(text: str) -> bytes:
    """把網址畫成 PNG。"""
    try:
        import io

        import qrcode

        img = qrcode.make(text, box_size=9, border=3)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 qrcode 套件：pip install 'qrcode[pil]'") from exc


def poll(key: str) -> dict[str, Any]:
    resp = requests.get(
        POLL,
        params={"qrcode_key": key, "source": "main-fe-header"},
        headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
        timeout=20,
    )
    payload = resp.json()
    data = payload.get("data") or {}
    code = data.get("code", payload.get("code"))
    status, message = STATUS.get(code, ("unknown", f"未知狀態 {code}"))
    result = {"status": status, "message": message, "code": code}
    if status == "ok":
        cookies = {c.name: c.value for c in resp.cookies}
        for header in resp.raw.headers.getlist("Set-Cookie") if hasattr(resp.raw, "headers") else []:
            piece = header.split(";", 1)[0]
            if "=" in piece:
                name, _, value = piece.partition("=")
                cookies.setdefault(name.strip(), value.strip())
        save_cookies(cookies)
        result["who"] = whoami()
    return result


def save_cookies(cookies: dict[str, str]) -> None:
    if not cookies.get("SESSDATA"):
        raise RuntimeError("沒有拿到 SESSDATA，登入沒成功")
    jar = http.cookiejar.MozillaCookieJar(str(cookie_file()))
    expires = int(time.time()) + 180 * 86400
    for name in COOKIE_FIELDS:
        value = cookies.get(name)
        if not value:
            continue
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0, name=name, value=value, port=None, port_specified=False,
                domain=".bilibili.com", domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True, secure=True, expires=expires,
                discard=False, comment=None, comment_url=None, rest={}, rfc2109=False,
            )
        )
    jar.save(ignore_discard=True, ignore_expires=True)
    state_file().write_text(
        json.dumps({"saved": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "who": cookies.get("DedeUserID", "")}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_jar() -> http.cookiejar.MozillaCookieJar | None:
    path = cookie_file()
    if not path.exists():
        return None
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception:  # noqa: BLE001
        return None
    return jar


def cookies_dict() -> dict[str, str]:
    jar = load_jar()
    return {c.name: c.value for c in jar} if jar else {}


def logged_in() -> bool:
    return bool(cookies_dict().get("SESSDATA"))


def whoami() -> dict[str, Any]:
    jar = load_jar()
    if not jar:
        return {"logged_in": False}
    resp = requests.get(
        "https://api.bilibili.com/x/web-interface/nav",
        headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
        cookies=jar,
        timeout=20,
    )
    data = resp.json().get("data") or {}
    return {
        "logged_in": bool(data.get("isLogin")),
        "name": data.get("uname") or "",
        "mid": data.get("mid") or "",
        "vip": (data.get("vipStatus") == 1),
    }


def logout() -> None:
    for path in (cookie_file(), state_file()):
        path.unlink(missing_ok=True)
