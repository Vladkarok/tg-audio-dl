FROM python:3.15-slim AS builder

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

# Copy dependency files
COPY pyproject.toml uv.lock* ./

# Install dependencies (no dev deps, frozen lock)
RUN uv sync --frozen --no-dev --no-install-project

# Production stage
FROM python:3.15-slim AS final

# ffmpeg for audio conversion. The JavaScript runtime yt-dlp needs for
# YouTube's challenges (deno) and its solver scripts (yt-dlp-ejs) come from
# uv.lock via the yt-dlp[default,pin-deno] extras, not from apt: Debian's nodejs
# is older than yt-dlp's minimum supported version.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
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
