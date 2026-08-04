# syntax=docker/dockerfile:1

# --- seed: parses ~10 MB of JSON, so it must run native (BUILDPLATFORM), never
# emulated under QEMU for the arm64 leg — the output SQLite file itself is
# architecture-independent, so there is no reason to pay the emulation tax twice.
FROM --platform=$BUILDPLATFORM python:3.12-slim AS seed

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN python -m daily_kotoba.seed --out /seed/kotoba-seed.db

# Fonts are fetched here (not vendored into the repo) and checksum-verified; the
# build fails outright on a mismatch rather than silently shipping a bad font.
RUN mkdir -p /fonts && \
    curl -fsSL -o /fonts/NotoSansJP-Regular.otf \
        https://github.com/notofonts/noto-cjk/raw/165c01b46ea533872e002e0785ff17e44f6d97d8/Sans/SubsetOTF/JP/NotoSansJP-Regular.otf && \
    curl -fsSL -o /fonts/NotoSansJP-Bold.otf \
        https://github.com/notofonts/noto-cjk/raw/165c01b46ea533872e002e0785ff17e44f6d97d8/Sans/SubsetOTF/JP/NotoSansJP-Bold.otf && \
    echo "dff723ba59d57d136764a04b9b2d03205544f7cd785a711442d6d2d085ac5073  /fonts/NotoSansJP-Regular.otf" | sha256sum -c - && \
    echo "1b0edfb500b73a4fa8a4fcaae1bbbd403994e08e73e3e0da37e70d3853f42c5f  /fonts/NotoSansJP-Bold.otf" | sha256sum -c -

# --- builder: target-platform venv with the runtime dependencies -----------------
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /bin/uv

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python .

# --- runtime -----------------------------------------------------------------
FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin app

ENV TZ=Australia/Sydney \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

COPY --from=builder /opt/venv /opt/venv
COPY --from=seed /seed/kotoba-seed.db /app/seed/kotoba-seed.db
COPY --from=seed /fonts /app/fonts

RUN mkdir -p /data && chown -R app:app /data /app

USER app
WORKDIR /app
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "daily_kotoba.api:app", "--host", "0.0.0.0", "--port", "8000"]
