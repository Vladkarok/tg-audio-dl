FROM python:3.13-slim AS builder

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.6.6 /uv /usr/local/bin/uv

# Copy dependency files
COPY pyproject.toml uv.lock* ./

# Install dependencies (no dev deps, frozen lock)
RUN uv sync --frozen --no-dev --no-install-project

# Production stage
FROM python:3.13-slim AS final

# Install ffmpeg and nodejs (ffmpeg for audio conversion, nodejs for yt-dlp JS challenge solving)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user with high UID to avoid collisions with host system users
RUN groupadd --gid 10001 botuser && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /bin/false botuser

WORKDIR /app

# Copy installed deps from builder
COPY --from=builder /app/.venv /app/.venv

# Copy source
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY pyproject.toml ./

# Create cache directory owned by botuser
RUN mkdir -p /app/cache && chown -R botuser:botuser /app/cache

# Switch to non-root
USER botuser

# Use venv python directly
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python", "-m", "src.main"]
