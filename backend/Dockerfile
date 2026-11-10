# Hubline backend image (Render Free, plan R9). Build context: backend/
#   docker build -t hubline-api backend
#   docker run --rm -p 8000:8000 --env-file backend/.env hubline-api
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8000

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first (cached layer), exactly as locked, without dev tools.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY scripts ./scripts

# Code and venv stay root-owned (read-only for the app); a chown -R here would double the image.
RUN useradd --create-home --uid 10001 hubline
USER hubline

EXPOSE 8000

# One process, one worker (the scheduler/worker objects live in-process; R9). Migrations run on
# every boot; Render's health check uses /healthz (never touches the DB).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
