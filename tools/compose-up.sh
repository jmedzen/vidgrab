#!/usr/bin/env bash
# docker compose 起 vidgrab，並帶入 git 的 build number（網頁左上角與 /api/version 會顯示）。
#
#   ./tools/compose-up.sh            # = docker compose up -d --build
#   ./tools/compose-up.sh logs -f    # 其他 compose 指令照轉
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ -z "${VIDGRAB_BUILD:-}" ]; then
  if n=$(git rev-list --count HEAD 2>/dev/null) && s=$(git rev-parse --short HEAD 2>/dev/null); then
    VIDGRAB_BUILD="$n.$s"
  else
    VIDGRAB_BUILD=dev
  fi
fi
export VIDGRAB_BUILD
echo "▸ vidgrab build $VIDGRAB_BUILD"

if [ $# -eq 0 ]; then set -- up -d --build; fi
exec docker compose "$@"
