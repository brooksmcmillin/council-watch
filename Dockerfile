FROM python:3.13-slim

WORKDIR /app

# Install uv (build-time only — not used at runtime)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Precompile bytecode at build time and put the venv on PATH so the runtime
# invokes alembic/uvicorn directly (no `uv run`, no writes to a uv cache —
# required for a read-only root filesystem in k8s).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# Install dependencies (this layer is cached until deps or sources change).
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN uv sync --frozen --no-dev

# Application files
COPY templates/ templates/
COPY static/ static/
COPY alembic.ini ./
COPY alembic/ alembic/

# Pre-create the writable data dir (a PVC mounts over /app/data in k8s) and run
# as a non-root user matching the deployment's securityContext (uid 1000).
RUN mkdir -p /app/data/pdfs \
    && useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER 1000

EXPOSE 8000

# Apply migrations against the mounted data volume, then serve.
CMD ["sh", "-c", "alembic upgrade head && uvicorn council_meetings.main:app --host 0.0.0.0 --port 8000"]
