# vidgrab — 關鍵字找素材（B 站／YouTube），整支或切片下載
#
#   docker build -t vidgrab .
#   docker run --rm -p 8310:8310 -v vidgrab-data:/data vidgrab
#   → http://127.0.0.1:8310
#
# 資料（專案、下載的素材、B 站 cookie）都放在 /data，掛一個 volume 就保住。
#
# 三個階段：wheel（打包）→ test（跑煙霧測試）→ runtime（只帶執行必需品）
# 測試一旦失敗，映像就建不起來，不會把壞掉的建置推到 registry。

# ---------- 1. 打包：寫入 build number 並做出 wheel ----------
FROM python:3.12-slim AS build

ARG VIDGRAB_BUILD=dev
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY tools/ ./tools/

# build number：優先用建置參數；給 dev 時嘗試從 .git 取（本機建置才有）
RUN if [ "$VIDGRAB_BUILD" = "dev" ]; then \
      python tools/stamp_build.py || true; \
    else \
      python tools/stamp_build.py --set "$VIDGRAB_BUILD"; \
    fi \
 && python -m pip install --upgrade pip build \
 && python -m build --wheel --outdir /dist

# ---------- 2. 測試：裝 wheel 後跑煙霧測試 ----------
FROM python:3.12-slim AS test

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=build /dist /dist
COPY tests/ /tests/

RUN python -m venv /test-venv \
 && /test-venv/bin/pip install --upgrade pip \
 && /test-venv/bin/pip install /dist/*.whl pytest \
 && cd / && /test-venv/bin/python -m pytest /tests -q

# ---------- 3. 執行階段 ----------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="vidgrab" \
      org.opencontainers.image.description="關鍵字找素材：搜 B 站／YouTube，整支或切片下載" \
      org.opencontainers.image.source="https://github.com/jmedzen/vidgrab"

# ffmpeg 是切片與併軌的必需品
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /dist /dist
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install /dist/*.whl \
 && rm -rf /dist

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIDGRAB_HOME=/data/projects \
    VIDGRAB_AUTH_DIR=/data/auth \
    VIDGRAB_HOST=0.0.0.0 \
    VIDGRAB_PORT=8310

# 容器內以非 root 執行；/data 通常由 volume 覆蓋，先建好並給權限
RUN useradd --create-home --uid 1000 vidgrab \
 && mkdir -p /data/projects /data/auth \
 && chown -R vidgrab:vidgrab /data

USER vidgrab
WORKDIR /data

EXPOSE 8310
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8310/api/version', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["vidgrab", "web", "--no-open"]
