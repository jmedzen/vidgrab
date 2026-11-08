#!/usr/bin/env bash
# 建 Docker 映像，順便把 git 的 build number 帶進去。
#
#   ./tools/docker-build.sh              # build = 2.8436bba
#   ./tools/docker-build.sh v1.0         # 自訂 tag 與 build
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

TAG="${1:-vidgrab:latest}"
BUILD="${VIDGRAB_BUILD:-}"

if [ -z "$BUILD" ]; then
  if BUILD=$(git rev-list --count HEAD 2>/dev/null) && SHA=$(git rev-parse --short HEAD 2>/dev/null); then
    BUILD="${BUILD}.${SHA}"
  else
    BUILD="dev"
  fi
fi

echo "▸ 建置 $TAG（build $BUILD）"
docker build --build-arg "VIDGRAB_BUILD=$BUILD" -t "$TAG" .

echo
echo "✔ 完成：$TAG"
echo "  跑起來：docker run --rm -p 127.0.0.1:8310:8310 -e VIDGRAB_NO_AUTH=1 -v vidgrab-data:/data $TAG"
echo "  開起來：http://127.0.0.1:8310"
