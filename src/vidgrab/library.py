"""素材庫：專案的顯示名稱、排序與釘選狀態。

專案本身是 `projects/<資料夾>/`，資料夾名不適合直接改（會動到所有引用路徑），
所以顯示名稱、排序、pin 這些「庫層級」的後設資料單獨存在 `projects/library.json`。

好處：
- 改名不會搬動任何檔案，下載紀錄與片段的相對路徑全部不受影響
- 一次讀寫整份索引，排序是單純的陣列順序
- 檔案不存在就當作全部用預設，向後相容舊的專案資料夾
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .models import read_json, write_json

LIBRARY_FILE = "library.json"


def _root() -> Path:
    from .tasks import projects_root

    return projects_root()


def _path(root: Path | None = None) -> Path:
    return (root or _root()) / LIBRARY_FILE


def load(root: Path | None = None) -> dict[str, Any]:
    """讀索引。壞掉就回空殼，不要讓整個介面起不來。"""
    data = read_json(_path(root), {}) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("order", [])
    data.setdefault("items", {})
    data.setdefault("sort", "manual")  # 目前的排序方式，跟著素材庫走
    if not isinstance(data["order"], list):
        data["order"] = []
    if not isinstance(data["items"], dict):
        data["items"] = {}
    return data


def save(data: dict[str, Any], root: Path | None = None) -> None:
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)


def entry(data: dict[str, Any], folder: str) -> dict[str, Any]:
    """單一專案的後設資料（沒有就用預設）。"""
    raw = data.get("items", {}).get(folder) or {}
    return {
        "folder": folder,
        "title": raw.get("title") or folder,
        "pinned": bool(raw.get("pinned")),
        "note": raw.get("note") or "",
        "hidden": bool(raw.get("hidden")),
    }


def sync(data: dict[str, Any], folders: list[str]) -> tuple[dict[str, Any], bool]:
    """讓索引與磁碟上的資料夾對齊：補新資料夾、移除已刪除的。

    回傳 (新索引, 有沒有變動)。
    """
    changed = False
    items = data.setdefault("items", {})
    known = set(items)

    for folder in folders:
        if folder not in items:
            items[folder] = {"title": folder, "pinned": False, "note": ""}
            changed = True

    for gone in known - set(folders):
        items.pop(gone, None)
        changed = True

    order = [f for f in data.get("order", []) if f in items]
    seen = set(order)
    for folder in folders:  # 保持既有順序，新的接在後面
        if folder not in seen:
            order.append(folder)
            changed = True
    if order != data.get("order"):
        data["order"] = order
        changed = True

    return data, changed


def rename(data: dict[str, Any], folder: str, title: str) -> dict[str, Any]:
    title = (title or "").strip()
    if not title:
        raise ValueError("名稱不能空白")
    if len(title) > 120:
        raise ValueError("名稱太長（最多 120 字）")
    data.setdefault("items", {}).setdefault(folder, {})["title"] = title
    return data


def set_pinned(data: dict[str, Any], folder: str, pinned: bool) -> dict[str, Any]:
    """釘選／取消釘選。

    釘選狀態一變，順序就要立刻重排，否則會出現「已釘選卻排在後面」的矛盾。
    排序模式不變，只把釘選項搬到最前面；取消釘選則落回未釘選區。
    """
    items = data.setdefault("items", {})
    was = bool((items.get(folder) or {}).get("pinned"))
    items.setdefault(folder, {})["pinned"] = bool(pinned)

    order = list(data.get("order", []))
    if folder not in order:
        order.append(folder)

    pinned_now = [f for f in order if (items.get(f) or {}).get("pinned")]
    rest = [f for f in order if f not in pinned_now]

    # 取消釘選時，不要留在最前面（那會看起來像還釘著），丟到未釘選區開頭
    if was and not pinned and folder in rest:
        rest.remove(folder)
        rest.insert(0, folder)

    data["order"] = pinned_now + rest
    return data


def set_order(data: dict[str, Any], order: list[str]) -> dict[str, Any]:
    """只接受目前存在的資料夾；沒提到的接在後面，不會遺失。"""
    items = data.get("items", {})
    clean = [f for f in order if f in items]
    seen = set(clean)
    for folder in data.get("order", []):
        if folder in items and folder not in seen:
            clean.append(folder)
            seen.add(folder)
    for folder in items:
        if folder not in seen:
            clean.append(folder)

    # 釘選的永遠在前：其餘維持原本的相對順序
    pinned = [f for f in clean if (items.get(f) or {}).get("pinned")]
    rest = [f for f in clean if f not in pinned]
    data["order"] = pinned + rest
    data["sort"] = "manual"  # 手動調整過就代表回到手動順序
    return data


def move(data: dict[str, Any], folder: str, delta: int) -> dict[str, Any]:
    """把某個專案往上／往下移一格。

    釘選的永遠在最前面，所以未釘選的不會越過任何一個釘選項。
    """
    items = data.get("items", {})
    order = list(data.get("order", []))
    if folder not in order:
        return data

    # 先把順序正規化成「釘選在前」，否則後面的位移計算會落在錯的位置
    pinned = [f for f in order if (items.get(f) or {}).get("pinned")]
    rest = [f for f in order if f not in pinned]
    order = pinned + rest
    npin = len(pinned)

    i = order.index(folder)
    target = i + delta
    if folder in pinned:
        target = max(0, min(npin - 1, target))
    else:
        target = max(npin, min(len(order) - 1, target))

    if target != i:
        order.pop(i)
        order.insert(target, folder)
    data["order"] = order
    return data


def sort_by(data: dict[str, Any], mode: str, stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """重新排序；pinned 的永遠在前面。

    mode: manual（目前順序）／name／created／clips／count
    """
    items = data.get("items", {})
    current = [f for f in data.get("order", []) if f in items]

    def pinned_first(seq: list[str]) -> list[str]:
        pin = [f for f in seq if (items.get(f) or {}).get("pinned")]
        rest = [f for f in seq if f not in pin]
        return pin + rest

    data["sort"] = mode
    if mode == "manual":
        data["order"] = pinned_first(current)
        return data

    if mode == "name":
        key = lambda f: (items.get(f, {}).get("title") or f).lower()  # noqa: E731
    elif mode == "created":
        key = lambda f: stats.get(f, {}).get("created") or ""  # noqa: E731
    elif mode == "clips":
        key = lambda f: -(stats.get(f, {}).get("clips") or 0)  # noqa: E731
    elif mode == "count":
        key = lambda f: -(stats.get(f, {}).get("count") or 0)  # noqa: E731
    else:
        raise ValueError(f"不認得的排序方式：{mode}")

    reverse = mode in ("created",)
    data["order"] = pinned_first(sorted(current, key=key, reverse=reverse))
    return data
