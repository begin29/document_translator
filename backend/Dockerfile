FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 fonts-dejavu-core fonts-dejavu-extra \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/

WORKDIR /app
COPY backend/pyproject.toml ./
RUN uv sync --no-dev --no-install-project
COPY backend/ ./
COPY models.yaml /app/models.yaml
RUN uv sync --no-dev --no-install-project

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FILES_DIR=/data \
    MODELS_CONFIG=/app/models.yaml

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
