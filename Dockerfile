# syntax=docker/dockerfile:1

# ---------- Stage 1: builder (installs dependencies into a venv) ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only requirements first so this layer is cached until they change.
COPY requirements.txt .
RUN pip install -r requirements.txt


# ---------- Stage 2: runtime (small, non-root, no build leftovers) ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Unprivileged user with a fixed numeric UID (lets Kubernetes verify runAsNonRoot).
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
 && mkdir /app \
 && chown app:app /app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app app ./app

USER 10001

EXPOSE 8000

# Docker-level healthcheck (Kubernetes ignores it and uses its own probes).
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

# Exec form: uvicorn is PID 1 and receives SIGTERM for graceful shutdown.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
