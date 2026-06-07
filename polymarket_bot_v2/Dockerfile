# ───────────────────────────────────────────────────────
# Dockerfile — PolyBot Production Container
# Optimized for Ireland (Dublin) low-latency deployment
# ───────────────────────────────────────────────────────

FROM python:3.11-slim-bookworm

# Set timezone to UTC (consistent with Polymarket)
ENV TZ=UTC
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install system deps
RUN apt-get update && apt-get install -y \
    gcc \
    libssl-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache optimization)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create data and log directories
RUN mkdir -p data logs

# Non-root user for security
RUN useradd -m polybot && chown -R polybot:polybot /app
USER polybot

# Healthcheck
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sqlite3; sqlite3.connect('data/polybot.db').execute('SELECT 1')" || exit 1

# Default: dryrun mode
CMD ["python", "main.py", "--mode", "dryrun", "--capital", "10"]
