# Optimized production image
FROM python:3.13-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/usr/local/share/playwright/browsers

# Install system dependencies and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt-dev \
    curl \
    ca-certificates \
    libxml2 \
    libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files for dependency installation
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install Python dependencies from pyproject.toml (cleaner than hardcoded versions)
RUN pip install --no-cache-dir . \
    && playwright install chromium \
    && playwright install-deps chromium \
    && apt-get update \
    && apt-get remove -y gcc libxml2-dev libxslt-dev \
    && apt-get autoremove -y \
    && apt-get autoclean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /tmp/* \
    && rm -rf /root/.cache

# Copy run script
COPY run.py ./

# Create non-root user and set up proper permissions for Playwright
RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && mkdir -p /home/appuser \
    && chown -R appuser:appuser /home/appuser \
    && chown -R appuser:appuser /usr/local/share/playwright

# Set ownership
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

EXPOSE 80

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:80/health || exit 1

# Run application
CMD ["python", "-m", "uvicorn", "src.downloader.main:app", "--host", "0.0.0.0", "--port", "80"]
