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

# Install ffmpeg (needed by yt-dlp for audio processing fallback)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --gid 1000 botuser && \
    useradd --uid 1000 --gid 1000 --no-create-home --shell /bin/false botuser

WORKDIR /app

# Copy uv binary
COPY --from=ghcr.io/astral-sh/uv:0.6.6 /uv /usr/local/bin/uv

# Copy installed deps from builder
COPY --from=builder /app/.venv /app/.venv

# Copy source
COPY src/ ./src/
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
