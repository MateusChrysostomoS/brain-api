# syntax=docker/dockerfile:1
#
# brain-api image (API only — no worker; there is no async/arq work).
# Mirrors the secretarIA build: uv + a layered dependency cache.

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 1. Dependencies only (cached layer — rebuilt only when deps change).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# 2. Application source + migrations, then install the project.
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

EXPOSE 8000

CMD ["uvicorn", "brain_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
