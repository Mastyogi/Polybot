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

# Copy source code and config
COPY polymarket_bot_v2/ ./polymarket_bot_v2/
COPY README.md .

# Install Python dependencies
RUN pip install --no-cache-dir -r polymarket_bot_v2/requirements.txt

# Create data and log directories
RUN mkdir -p data logs

# Set entrypoint
CMD ["python", "polymarket_bot_v2/main.py", "--mode", "paper", "--capital", "10"]
