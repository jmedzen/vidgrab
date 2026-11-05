#!/usr/bin/env python3
"""把 build number 寫進套件裡，讓沒有 .git 的環境（Docker）也知道自己是哪一版。

用法：
    python tools/stamp_build.py            # 自動取 git 資訊
    python tools/stamp_build.py --set 12.abc1234
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "src" / "vidgrab" / "_build_info.py"


def from_git() -> str:
    try:
        count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        return f"{count}.{sha}"
    except Exception:  # noqa: BLE001
        return "dev"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", dest="value", help="直接指定 build number")
    args = parser.parse_args()

    value = args.value or from_git()
    TARGET.write_text(
        '"""由 tools/stamp_build.py 自動產生，請勿手改。"""\n\n'
        f'BUILD = "{value}"\n',
        encoding="utf-8",
    )
    print(f"build = {value}  →  {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
