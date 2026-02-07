# Base Sleuth — Production image
# Multi-stage build for minimal final image

FROM python:3.12-slim AS base

# Prevent Python from writing bytecode / buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps for asyncpg (PostgreSQL C client) and lxml
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

# ── Dependencies ──────────────────────────────────────────────
FROM base AS deps

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application ───────────────────────────────────────────────
FROM deps AS app

# Copy application code
COPY clanker_tracker/ clanker_tracker/
COPY alembic/ alembic/
COPY alembic.ini .
COPY config.example.yaml config.example.yaml
COPY data/ data/

# Create logs directory
RUN mkdir -p logs

# Non-root user for security
RUN groupadd -r sleuth && useradd -r -g sleuth sleuth && \
    chown -R sleuth:sleuth /app
USER sleuth

# Health check — verify Python can import the package
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "from clanker_tracker.main import Tracker; print('ok')"

# Default: run migrations then start the bot
ENTRYPOINT ["sh", "-c", "python -m alembic upgrade head && python -m clanker_tracker.main"]
