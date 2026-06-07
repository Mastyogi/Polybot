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
COPY polymarket_bot_v2/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy entire repo structure (preserves polymarket_bot_v2 subdirectory)
COPY . .

# Create data and log directories
RUN mkdir -p data logs

# Set entrypoint
CMD ["python", "polymarket_bot_v2/main.py", "--mode", "paper", "--capital", "10"]
