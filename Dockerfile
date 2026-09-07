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

RUN uv sync --frozen --no-dev \
    && mkdir -p /app/data \
    && useradd --system --uid 10001 --create-home appuser \
    && chown -R appuser:appuser /app

ENV BUNKR_DB=/app/data/bunkr.db
EXPOSE 8000

USER appuser
# Default command serves the UI; compose overrides it for the sync service.
CMD ["python", "-m", "bunkr_index", "serve", "--host", "0.0.0.0", "--port", "8000"]
