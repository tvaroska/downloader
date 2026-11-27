# Optimized production image
FROM python:3.13-slim@sha256:7249d5587b78209a006dc4a3b5340678f84ea729239193aa559962cd8c1fce50

COPY --from=ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a /uv /uvx /bin/

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/usr/local/share/playwright/browsers \
    PATH="/app/.venv/bin:$PATH"

# Install system dependencies and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt-dev \
    ca-certificates \
    libxml2 \
    libxslt1.1 \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy locked project metadata and source for deterministic dependency installation
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# Install locked Python dependencies and Playwright Chromium
RUN uv sync --frozen --no-dev --no-editable \
    && playwright install chromium \
    && playwright install-deps chromium \
    && apt-get update \
    && apt-get upgrade -y \
    && apt-get remove -y gcc libxml2-dev libxslt-dev \
    && apt-get autoremove -y \
    && apt-get autoclean \
    && /usr/local/bin/python3 -m pip uninstall -y pip setuptools wheel \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip* \
              /usr/local/lib/python3.13/site-packages/setuptools* \
              /usr/local/lib/python3.13/site-packages/wheel* \
              /usr/local/lib/python3.13/ensurepip \
              /usr/local/bin/pip* \
    && rm -f /bin/uv /bin/uvx \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /tmp/* \
    && rm -rf /root/.cache

# Copy run script
COPY run.py ./

# Create non-root user and keep /app root-owned (read-only for appuser)
RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && mkdir -p /home/appuser \
    && chown -R appuser:appuser /home/appuser \
    && chown -R appuser:appuser /usr/local/share/playwright \
    && chown -R root:root /app \
    && chmod -R a+rX,go-w /app

# Switch to non-root user
USER appuser

EXPOSE 8000

# Health check via stdlib urllib.request
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read()"]

# Run application with graceful connection draining budget
CMD ["python", "-m", "uvicorn", "downloader.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "35"]
