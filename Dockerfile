# syntax=docker/dockerfile:1
# Render / container image for the NASA Hackathime demo API.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    JARVIS_HOST=0.0.0.0 \
    JARVIS_ALLOW_NON_LOOPBACK=true \
    JARVIS_DEMO_MODE=true \
    JARVIS_APP_ENV=production \
    JARVIS_TTS_ENABLED=false \
    JARVIS_TTS_AUTOSTART=false \
    JARVIS_OLLAMA_WARM_ON_BOOT=false

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY config ./config
COPY demo ./demo

RUN uv sync --frozen --no-dev

# Sample vault + demo policy ship in the image; personal vaults must not.
EXPOSE 8756

CMD ["sh", "-c", "uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8756}"]
