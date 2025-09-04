# Use Python 3.11 slim image for smaller size and security
FROM python:3.11-slim

# Set environment variables for Python optimization
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_CACHE_DIR=/tmp/uv-cache

# Install system dependencies needed for lxml, Playwright and other packages
RUN apt-get update && apt-get install -y \
    libxml2-dev \
    libxslt-dev \
    gcc \
    curl \
    wget \
    ca-certificates \
    fonts-liberation \
    libappindicator3-1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils \
    libgbm1 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Set work directory
WORKDIR /app

# Install uv package manager
RUN pip install uv

# Copy dependency files and README (required by pyproject.toml)
COPY pyproject.toml uv.lock README.md ./

# Install dependencies only (without the package itself)
RUN uv pip install --system fastapi uvicorn httpx pydantic beautifulsoup4 lxml playwright

# Copy application code
COPY src/ ./src/
COPY run.py ./

# Create cache directory, home directory, and change ownership to non-root user
RUN mkdir -p /tmp/uv-cache /home/appuser && \
    chown -R appuser:appuser /app /tmp/uv-cache /home/appuser

# Switch to non-root user
USER appuser

# Set PLAYWRIGHT_BROWSERS_PATH to a location the user can write to
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright

# Install Playwright browsers as the non-root user
RUN playwright install chromium

# Expose port
EXPOSE 80

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:80/health || exit 1

# Run the application (production mode without reload)
CMD ["python", "-m", "uvicorn", "src.downloader.main:app", "--host", "0.0.0.0", "--port", "80"]