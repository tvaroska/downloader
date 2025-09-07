# Build dependencies and runtime in single stage (optimized)
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/usr/local/share/playwright/browsers

# Install dependencies + build tools, install Python packages + Playwright deps, then remove build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt-dev \
    curl \
    ca-certificates \
    libxml2 \
    libxslt1.1 \
    && pip install --no-cache-dir \
        fastapi>=0.104.0 \
        uvicorn[standard]>=0.24.0 \
        httpx>=0.25.0 \
        pydantic>=2.0.0 \
        python-multipart>=0.0.6 \
        beautifulsoup4>=4.13.5 \
        lxml>=6.0.1 \
        playwright>=1.40.0 \
    && playwright install chromium \
    && playwright install-deps chromium \
    && apt-get remove -y gcc libxml2-dev libxslt-dev \
    && apt-get autoremove -y \
    && apt-get autoclean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /tmp/* \
    && rm -rf /root/.cache

# Create non-root user and set up proper permissions for Playwright
RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && mkdir -p /home/appuser \
    && chown -R appuser:appuser /home/appuser \
    && chown -R appuser:appuser /usr/local/share/playwright

WORKDIR /app

# Copy application
COPY src/ ./src/
COPY run.py ./

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