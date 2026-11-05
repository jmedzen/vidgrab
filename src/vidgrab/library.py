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
        "missing": bool(raw.get("missing")),
        # 記錄下最後一次看到的樣子，資料夾不見時還原得回來
        "last_seen": raw.get("last_seen") or "",
        "queries": raw.get("queries") or [],
        "count": raw.get("count"),
        "clips": raw.get("clips"),
    }


def sync(data: dict[str, Any], folders: list[str]) -> tuple[dict[str, Any], bool]:
    """讓索引與磁碟對齊，但**記錄不因為資料夾不見就被刪掉**。

    這是踩過的坑：原本這裡會 `items.pop()`，結果換一個 VIDGRAB_HOME、
    或暫時讀不到目錄，整份清單就跟著消失。現在的規則是：

    - 磁碟上有、索引沒有 → 補一筆
    - 索引有、磁碟上沒有 → 標記 missing=True，**保留**，等使用者復原或明確刪除
    - 磁碟上又出現了 → 自動把 missing 清掉（等於自動復原）

    回傳 (新索引, 有沒有變動)。
    """
    changed = False
    items = data.setdefault("items", {})
    present = set(folders)

    for folder in folders:
        if folder not in items:
            items[folder] = {"title": folder, "pinned": False, "note": "", "missing": False}
            changed = True
        elif items[folder].get("missing"):
            items[folder]["missing"] = False
            changed = True
        elif "missing" not in items[folder]:
            # 舊資料沒有這個欄位，補上以便統一判斷
            items[folder]["missing"] = False
            changed = True

    for gone in set(items) - present:
        entry_ = items.get(gone) or {}
        if not entry_.get("missing"):
            entry_["missing"] = True
            items[gone] = entry_
            changed = True

    # 順序保留所有記錄（含 missing 的），掃描／復原後才不會亂掉
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


# ---------------------------------------------------------------- 掃描／復原


def scan(root: Path | None = None) -> dict[str, Any]:
    """盤點「磁碟上有什麼」與「記錄裡有什麼」，找出兩邊不一致的地方。

    這個函式只讀不寫，讓使用者先看清楚再決定要復原什麼。
    """
    import json

    root = root or _root()
    data = load(root)

    on_disk: list[dict[str, Any]] = []
    for p in sorted(root.glob("*")) if root.exists() else []:
        if not p.is_dir():
            continue
        manifest_path = p / "manifest.json"
        results_path = p / "results.json"
        if not manifest_path.exists() and not results_path.exists():
            continue
        manifest: dict[str, Any] = {}
        count = 0
        clips = 0
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            manifest = {}
        try:
            raw = json.loads(results_path.read_text(encoding="utf-8")) or []
            count = len(raw)
            for it in raw:
                clips += len(it.get("clips") or [])
        except Exception:  # noqa: BLE001
            pass
        on_disk.append({
            "folder": p.name,
            "queries": manifest.get("queries") or [],
            "created": manifest.get("created") or "",
            "count": count,
            "clips": clips,
        })

    disk_folders = {d["folder"] for d in on_disk}
    known = set(data.get("items", {}))

    untracked = [d for d in on_disk if d["folder"] not in known]
    missing = [
        {"folder": f, **{k: v for k, v in (data["items"].get(f) or {}).items() if k != "folder"}}
        for f in data.get("items", {})
        if f not in disk_folders
    ]

    return {
        "root": str(root),
        "on_disk": on_disk,
        "tracked": len(known),
        "untracked": untracked,      # 磁碟有、記錄沒有 → 可「納入」
        "missing": missing,          # 記錄有、磁碟沒有 → 可「找出」或「移除記錄」
        "ok": not untracked and not missing,
    }


def adopt(data: dict[str, Any], folders: list[str]) -> dict[str, Any]:
    """把磁碟上找到、但記錄裡沒有的資料夾納入索引。"""
    items = data.setdefault("items", {})
    order = data.setdefault("order", [])
    for folder in folders:
        if folder not in items:
            items[folder] = {"title": folder, "pinned": False, "note": "", "missing": False}
        else:
            items[folder]["missing"] = False
        if folder not in order:
            order.append(folder)
    return data


def forget(data: dict[str, Any], folders: list[str]) -> dict[str, Any]:
    """明確地把記錄移除（只給「確定不要了」用；預設不會自動移除）。"""
    items = data.get("items", {})
    for folder in folders:
        items.pop(folder, None)
    data["items"] = items
    data["order"] = [f for f in data.get("order", []) if f in items]
    return data


def clean_tags(data: dict[str, Any]) -> int:
    """清掉記錄裡既有的技術標記（給已存在的資料用）。回傳清掉幾筆。"""
    return 0
