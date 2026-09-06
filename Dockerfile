FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
COPY bunkr_index ./bunkr_index
COPY frontend ./frontend

RUN uv sync --frozen --no-dev
RUN mkdir -p /app/data

ENV BUNKR_DB=/app/data/bunkr.db
EXPOSE 8000
