"""版本與 build number。

build number 的來源優先序：
1. 環境變數 VIDGRAB_BUILD（Docker 建置時帶入，最可靠）
2. 打包時產生的 _build_info.py（tools/stamp_build.py 寫入）
3. git 的 commit 數與短 hash（本機開發）
4. 都沒有就退回版本號本身

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
    return _from_env() or _from_file() or _from_git() or "dev"


@lru_cache(maxsize=1)
def full_version() -> str:
    """像 0.2.0+12.8436bba 這樣。"""
    b = build()
    return f"{__version__}+{b}" if b and b != "dev" else __version__


def banner() -> str:
    return f"vidgrab {full_version()}"
