# LlamaShift - Universal LLM Workstation Manager
# Multi-stage Dockerfile for cross-platform deployment

# ─── Build stage (not needed for Python, but kept for extensibility) ───
FROM python:3.11-slim AS base

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || true

# Copy application files
COPY server.py .
COPY mcp_server.py .
COPY config.json .
COPY static/ ./static/

# Create logs directory
RUN mkdir -p /root/logs/llamashift

# Expose the backend API port
EXPOSE 8002

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8002/api/config || exit 1

# Run the server
CMD ["python", "server.py"]