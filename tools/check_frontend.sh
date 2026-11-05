#!/usr/bin/env bash
# 確認版控裡的前端產物跟 web/src 一致（避免改了原始碼忘記 build）。
# 用法：tools/check_frontend.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v node >/dev/null 2>&1; then
  echo "⚠ 沒有 node，跳過前端一致性檢查"
  exit 0
fi

stamp="src/vidgrab/static/.build-stamp"
if [ ! -f "$stamp" ]; then
  echo "✗ 找不到 $stamp —— 從沒 build 過？"
  exit 1
fi

current=$(cd web/src && find . -type f \( -name '*.ts' -o -name '*.tsx' -o -name '*.css' \) -print0 \
          | sort -z | xargs -0 cat | shasum -a 256 | cut -d' ' -f1)

if [ "$current" != "$(cat "$stamp")" ]; then
  echo "✗ 前端原始碼改了，但 static/ 裡的產物沒重新 build。"
  echo "  修正：cd web && npm run build"
  exit 1
fi

echo "✔ 前端產物與原始碼一致"
