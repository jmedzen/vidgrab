"""設定：一旦存過就變成預設值，CLI 與 Web 共用同一份。

存在資料根目錄的 config.json（Docker 裡就是 /data/config.json），
所以只要掛著同一個 volume，重開容器設定也還在。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# 可設定的項目與預設值。新增選項時只要往這裡加，CLI 與 API 會自動吃到。
DEFAULTS: dict[str, Any] = {
    # 搜尋
    "platform": "both",
    "per_keyword": 12,
    "order": "smart",
    "duration": "any",
    "min_duration": 10.0,
    "max_duration": 0.0,
    "fresh_days": 0,
    "min_views": 0,
    "exclude": [],
    "only_free": False,
    "drop_reposts": False,
    "verify_license": False,
    "sort": "smart",
    # 下載
    "per": 15.0,
    "height": 1080,
    "audio": False,
    "audio_format": "m4a",
    "frames": True,
    "frame_count": 3,
    # 服務
    "host": "127.0.0.1",
    "port": 8310,
}

# 型別檢查用：設定值一定要是這幾種，避免外部 API 塞奇怪的東西進來
TYPES = {
    "platform": str,
    "per_keyword": int,
    "order": str,
    "duration": str,
    "min_duration": float,
    "max_duration": float,
    "fresh_days": int,
    "min_views": int,
    "exclude": list,
    "only_free": bool,
    "drop_reposts": bool,
    "verify_license": bool,
    "sort": str,
    "per": float,
    "height": int,
    "audio": bool,
    "audio_format": str,
    "frames": bool,
    "frame_count": int,
    "host": str,
    "port": int,
}

ENUMS = {
    "platform": {"both", "bilibili", "youtube"},
    "order": {"smart", "relevance", "views", "new"},
    "duration": {"any", "short", "medium", "long", "verylong"},
    "sort": {"smart", "license", "views", "new"},
    "audio_format": {"m4a", "mp3", "wav", "opus"},
}


class ConfigError(ValueError):
    pass


def config_path() -> Path:
    explicit = os.environ.get("VIDGRAB_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    # 放在 auth 的上一層：Docker 是 /data/config.json，本機是專案根目錄
    from . import auth

    return auth.home().parent / "config.json"


def load() -> dict[str, Any]:
    """讀設定；壞掉就回預設，不要讓整個工具起不來。"""
    path = config_path()
    data = dict(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return data
    if isinstance(raw, dict):
        # 存檔格式是 {"updated": ..., "values": {...}}；也接受扁平的舊格式
        values = raw.get("values") if isinstance(raw.get("values"), dict) else raw
        for key, value in values.items():
            if key in DEFAULTS:
                data[key] = value
    return data


def save(values: dict[str, Any]) -> dict[str, Any]:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "values": values,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return values


def validate(key: str, value: Any) -> Any:
    """把外部傳進來的值轉成型別並檢查範圍。"""
    if key not in DEFAULTS:
        raise ConfigError(f"不認得的設定項目：{key}")
    want = TYPES[key]
    try:
        if want is bool:
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on", "是")
            else:
                value = bool(value)
        elif want is int:
            value = int(value)
        elif want is float:
            value = float(value)
        elif want is str:
            value = str(value).strip()
        elif want is list:
            if isinstance(value, str):
                value = [t.strip() for t in value.split(",") if t.strip()]
            elif isinstance(value, (list, tuple)):
                value = [str(t).strip() for t in value if str(t).strip()]
            else:
                raise ConfigError(f"{key} 要是一串字或陣列")
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} 的值不對：{value!r}") from exc

    if key in ENUMS and value not in ENUMS[key]:
        raise ConfigError(f"{key} 只能是 {'/'.join(sorted(ENUMS[key]))}")
    if key in ("per_keyword", "frame_count", "height", "port") and value <= 0:
        raise ConfigError(f"{key} 要大於 0")
    if key in ("min_duration", "max_duration", "per") and value < 0:
        raise ConfigError(f"{key} 不能是負數")
    if key == "port" and not (1 <= value <= 65535):
        raise ConfigError("port 要在 1–65535 之間")
    return value


def save_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """只改傳進來的欄位，其餘保持不動。"""
    current = load()
    for key, value in patch.items():
        current[key] = validate(key, value)
    return save(current)


def reset(keys: list[str] | None = None) -> dict[str, Any]:
    """還原成出廠預設；給 keys 就只還原那幾項。"""
    current = load()
    if not keys:
        current = dict(DEFAULTS)
    else:
        for key in keys:
            if key not in DEFAULTS:
                raise ConfigError(f"不認得的設定項目：{key}")
            current[key] = DEFAULTS[key]
    return save(current)


def describe() -> dict[str, Any]:
    return {
        "path": str(config_path()),
        "values": load(),
        "defaults": DEFAULTS,
    }
