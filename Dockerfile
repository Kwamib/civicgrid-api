# syntax=docker/dockerfile:1.7

# =========================================
# Stage 1: builder
# Installs Python deps into a virtualenv we'll copy into the final image.
# This stage has compilers and headers; the final image won't.
# =========================================
FROM python:3.13-slim AS builder

# Don't write .pyc files; don't buffer stdout/stderr. Standard for containers.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install system build deps needed for psycopg2-binary
# (binary wheel usually works without these, but keep them for safety on some arches)
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libpq-dev \
 && rm -rf /var/lib/apt/lists/*

# Copy ONLY requirements first so dep install layer is cached
# until requirements actually change. Code changes won't bust this layer.
COPY requirements.txt .

# Build a self-contained virtualenv at /opt/venv with all deps installed.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


# =========================================
# Stage 2: runtime
# Minimal image: Python + the venv from the builder + our code.
# No compilers, no apt cache, no build artifacts.
# =========================================
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Runtime needs libpq5 (the C library psycopg2 talks to Postgres through).
# We do NOT need libpq-dev or gcc here.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/*

# Create a non-root user so the container doesn't run as root.
# Best practice and required by some K8s security policies (PodSecurity Standards).
RUN groupadd --system --gid 1000 app \
 && useradd  --system --uid 1000 --gid app --create-home --home-dir /home/app app

WORKDIR /app

# Copy the virtualenv from the builder stage. This is the multi-stage win:
# the builder's gcc/libpq-dev/apt-cache stays behind in stage 1 and never
# ships to production.
COPY --from=builder /opt/venv /opt/venv

# Copy application source. Owned by the app user, not root.
COPY --chown=app:app app /app/app

USER app

EXPOSE 8000

# Healthcheck so Docker (and orchestrators) know when the container is actually ready.
# We hit our own /health endpoint via Python (no curl/wget in slim image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
                   r=urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3); \
                   sys.exit(0 if r.status==200 else 1)" || exit 1

# Production-style command. No --reload (that's for dev only).
# Bind to 0.0.0.0 so the container accepts connections from outside itself.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]