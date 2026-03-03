FROM python:3.13-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY templates/ templates/
COPY static/ static/
COPY alembic.ini alembic/

# Install dependencies and create data directory
RUN uv sync --frozen --no-dev && mkdir -p /app/data/pdfs

# Copy alembic config
COPY alembic/ alembic/

EXPOSE 8000

# Run migrations then start the app
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn council_meetings.main:app --host 0.0.0.0 --port 8000"]
