#!/usr/bin/env python3
"""靜態檢查 Dockerfile 與建置 context，抓出「照著跑會失敗」的問題。

驗的是：
  1. Dockerfile 每個本機 COPY 的來源，是否被 .dockerignore 排掉
  2. ENV/ARG 是否用了未宣告的變數（會默默展開成空字串）
  3. 多階段建置的 --from 是否指向真的存在的階段
  4. ENTRYPOINT/CMD 指向的執行檔，是否真的會被裝進映像

沒有 Docker 也能跑，CI 裡先跑這支可以省掉一次失敗的建置。
"""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KNOWN_PROJECT_SCRIPTS = {"vidgrab"}  # pyproject 的 [project.scripts]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"


def parse_instructions(text: str) -> list[tuple[str, str, int]]:
    """把續行接起來，回傳 (指令, 內容, 行號)。"""
    out: list[tuple[str, str, int]] = []
    buf = ""
    start = 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not buf:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            start = n
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        stripped = buf.strip()
        if stripped:
            parts = stripped.split(None, 1)
            out.append((parts[0].upper(), parts[1] if len(parts) > 1 else "", start))
        buf = ""
    return out


def ignore_patterns(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        ln.strip() for ln in path.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def is_ignored(rel: str, patterns: list[str]) -> bool:
    """依 Docker 的規則：後面符合的樣式會蓋掉前面的（含 ! 否定）。"""
    ignored = False
    for pat in patterns:
        negate = pat.startswith("!")
        body = pat[1:] if negate else pat
        body = body.strip("/")
        matched = (
            fnmatch.fnmatch(rel, body)
            or fnmatch.fnmatch(Path(rel).name, body)
            or rel.startswith(body + "/")
        )
        if matched:
            ignored = not negate
    return ignored


def main() -> int:
    if not DOCKERFILE.exists():
        print("找不到 Dockerfile")
        return 1

    insts = parse_instructions(DOCKERFILE.read_text())
    patterns = ignore_patterns(DOCKERIGNORE)
    errors: list[str] = []
    warnings: list[str] = []

    stages: list[str] = []
    for k, c, _ in insts:
        if k != "FROM":
            continue
        parts = c.split()
        if len(parts) >= 3 and parts[1].upper() == "AS":
            stages.append(parts[2].lower())
        elif len(parts) >= 2:
            stages.append(parts[0].lower())  # 沒有命名，用映像名當識別
    declared_args: set[str] = set()
    env_names: set[str] = set()

    for kind, content, line in insts:
        if kind == "ARG":
            name = content.split("=")[0].strip()
            if name:
                declared_args.add(name)

        elif kind == "ENV":
            for token in content.split():
                if "=" in token:
                    env_names.add(token.split("=", 1)[0])
                    value = token.split("=", 1)[1]
                    for ref in re.findall(r"\$\{(\w+)\}", value):
                        if ref not in declared_args and ref not in env_names:
                            errors.append(
                                f"L{line}: ENV 用了未宣告的 ${{{ref}}}，會展開成空字串"
                            )

        elif kind in ("COPY", "ADD"):
            if content.startswith("--from"):
                rest = content[len("--from"):].lstrip("=")
                src_stage = rest.split()[0].strip().lower()
                if src_stage not in stages and not src_stage.isdigit():
                    errors.append(f"L{line}: COPY --from={src_stage} 找不到對應的建置階段")
                continue
            tokens = [t for t in content.split() if not t.startswith("--")]
            if len(tokens) < 2:
                continue
            for src in tokens[:-1]:
                if src.startswith(("http://", "https://")):
                    continue
                target = ROOT / src.rstrip("/")
                if not target.exists():
                    errors.append(f"L{line}: COPY 來源不存在：{src}")
                    continue
                if is_ignored(src.strip("./"), patterns):
                    errors.append(
                        f"L{line}: COPY 來源 {src} 被 .dockerignore 排除了，建置會失敗"
                    )

        elif kind in ("ENTRYPOINT", "CMD"):
            # 只檢查「第一個元素」＝實際被執行的執行檔
            m = re.search(r'[\["]\s*"([^"]+)"', content)
            if not m:
                continue
            exe = m.group(1)
            if exe.startswith("/"):
                continue  # 絕對路徑（如 tini）由 base image 提供
            if exe in KNOWN_PROJECT_SCRIPTS:
                continue  # pyproject 宣告過，pip 裝完就有
            warnings.append(f"L{line}: 找不到 {exe} 的來源（既沒 COPY 進來，也不是已宣告的指令）")

    # 檢查 .dockerignore 有沒有把關鍵目錄排掉
    for need in ("src", "tests", "tools", "pyproject.toml"):
        if is_ignored(need, patterns):
            errors.append(f".dockerignore 排除了 {need}，但建置需要它")

    if warnings:
        print("警告：")
        for w in warnings:
            print(f"  · {w}")
    if errors:
        print("錯誤：")
        for e in errors:
            print(f"  ✗ {e}")
        return 1

    print(f"✔ Dockerfile 檢查通過（{len(stages)} 個階段：{' → '.join(stages)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
