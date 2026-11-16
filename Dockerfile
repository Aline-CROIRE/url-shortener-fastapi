# syntax=docker/dockerfile:1

# ---------- Stage 1: builder ----------
# Installs the Python packages into a virtual environment.
FROM python:3.14-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only requirements first: Docker caches this layer, so packages are
# reinstalled only when requirements.txt changes, not on every code change.
COPY requirements.txt .
RUN pip install -r requirements.txt


# ---------- Stage 2: runtime ----------
# Small final image: only the venv and our code, no build leftovers.
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# A normal (non-root) user, so a bug in the app cannot take over the container
RUN groupadd --system app && useradd --system --gid app --no-create-home app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app app ./app
COPY --chown=app:app gunicorn.conf.py .
# Database migrations, so the container can run "alembic upgrade head"
COPY --chown=app:app alembic.ini .
COPY --chown=app:app alembic ./alembic

USER app

EXPOSE 8000

# Docker marks the container "unhealthy" if /health fails (e.g. Redis is down -> 503)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"]

# Default = production: Gunicorn managing Uvicorn workers
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app.main:app"]
