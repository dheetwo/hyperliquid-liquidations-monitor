# Hyperdash Liquidation Monitor
# For deployment on Coolify/Hetzner

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies (procps for health check pgrep)
RUN apt-get update && apt-get install -y --no-install-recommends \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for Docker cache)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create logs and data directories
RUN mkdir -p logs data/raw data/processed

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Run the monitor service
CMD ["python", "scripts/run_monitor.py"]
