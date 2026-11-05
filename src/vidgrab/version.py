"""版本與 build number。

build number 的來源優先序：
1. 環境變數 VIDGRAB_BUILD（Docker 建置時帶入，最可靠）
2. git 的 commit 數與短 hash（本機開發：永遠是最新的）
3. 打包時產生的 _build_info.py（沒有 .git 的環境才靠這個）
4. 都沒有就退回版本號本身

**為什麼 git 要排在 _build_info.py 前面**：
那個檔案是 stamp 時寫下的快照，一旦存在就會永遠蓋掉 git，
於是明明已經有 11 個 commit，`./vidgrab build` 還一直顯示 3。
Docker 裡沒有 .git，所以第 1、3 條仍然能正確報版。

刻意不 import subprocess 之外的東西，讓它永遠不會因為環境怪而爆掉。
"""

from __future__ import annotations

import os
from functools import lru_cache

__version__ = "0.2.0"


def _from_env() -> str:
    return os.environ.get("VIDGRAB_BUILD", "").strip()


def _from_file() -> str:
    try:
        from ._build_info import BUILD  # type: ignore[attr-defined]

        return str(BUILD).strip()
    except Exception:  # noqa: BLE001
        return ""


def _from_git() -> str:
    try:
        import subprocess

        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=3,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=3,
        )
        if count.returncode == 0 and sha.returncode == 0:
            return f"{count.stdout.strip()}.{sha.stdout.strip()}"
    except Exception:  # noqa: BLE001
        pass
    return ""


@lru_cache(maxsize=1)
def build() -> str:
    """目前這份程式碼是哪一版。

    順序刻意是 env → git → 檔案：
    有 git 時就用 git（最新），沒有 git（Docker／pip 安裝）才看 stamp 檔。
    """
    return _from_env() or _from_git() or _from_file() or "dev"


@lru_cache(maxsize=1)
def full_version() -> str:
    """像 0.2.0+12.8436bba 這樣。"""
    b = build()
    return f"{__version__}+{b}" if b and b != "dev" else __version__


def banner() -> str:
    return f"vidgrab {full_version()}"
