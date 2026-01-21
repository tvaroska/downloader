# Deployment Guide

This guide covers deploying the REST API Downloader to production.

## Prerequisites

- **Docker** 20.10+ and **Docker Compose** 2.0+
- **Redis** 7+ (optional, enables batch job persistence)
- **RAM**: 2GB minimum, 4GB+ recommended
- **CPU**: 2+ cores recommended

## Quick Start

### Development

```bash
docker-compose up
```

Access the API at `http://localhost:8000`. Swagger UI available at `/docs`.

### Production

```bash
# Copy and configure environment
cp .env.example .env
# Edit .env with production values (see Configuration section)

# Deploy with production overlay
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Access the API at `http://localhost:80`.

## Configuration

All settings have sensible defaults. See `.env.example` for the complete list.

### Security-Critical Settings

| Variable | Default | Production Recommendation |
|----------|---------|---------------------------|
| `DOWNLOADER_KEY` | None (auth disabled) | Set a strong API key |
| `CORS_ALLOWED_ORIGINS` | localhost only | **REQUIRED**: Set to your production domains |
| `ENVIRONMENT` | `development` | Set to `production` |

> **CORS Security Warning**: The default CORS configuration only allows localhost
> origins for development convenience. In production, you **MUST** explicitly configure
> `CORS_ALLOWED_ORIGINS` with your actual domains (e.g., `https://yourdomain.com`).
> Using `*` (wildcard) is strongly discouraged as it allows any website to make
> requests to your API.

### Production .env Example

```bash
# Application
ENVIRONMENT=production

# Security
DOWNLOADER_KEY=your-secret-api-key-minimum-32-characters
CORS_ALLOWED_ORIGINS=https://yourdomain.com,https://api.yourdomain.com

# Redis (enables batch processing)
REDIS_URI=redis://redis:6379

# Logging
LOG_JSON_LOGS=true
LOG_LEVEL=INFO

# Performance (adjust based on your resources)
PDF_CONCURRENCY=8
BATCH_CONCURRENCY=32
```

### Resource Sizing

**Memory-constrained (2GB RAM)**:
```bash
PDF_CONCURRENCY=2
BATCH_CONCURRENCY=8
PDF_POOL_SIZE=1
HTTP_MAX_CONNECTIONS=40
```

**High-performance (16GB RAM, 8+ cores)**:
```bash
PDF_CONCURRENCY=16
BATCH_CONCURRENCY=64
HTTP_MAX_CONNECTIONS=400
```

## Deployment Methods

### Docker Compose (Recommended)

The production overlay (`docker-compose.prod.yml`) adds:
- Resource limits (2 CPU, 2GB RAM for app; 0.5 CPU, 512MB for Redis)
- Restart policies
- Port 80 mapping

```bash
# Start
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# View logs
docker-compose logs -f app

# Stop
docker-compose -f docker-compose.yml -f docker-compose.prod.yml down
```

### Docker Standalone

```bash
# Build
docker build -t downloader .

# Run (with Redis)
docker run -d \
  --name downloader \
  -p 80:80 \
  -e ENVIRONMENT=production \
  -e DOWNLOADER_KEY=your-api-key \
  -e REDIS_URI=redis://redis-host:6379 \
  downloader
```

### Direct Python (Development Only)

```bash
# Install dependencies
pip install -e .
playwright install chromium

# Run
uvicorn src.downloader.main:app --host 0.0.0.0 --port 8000
```

## Health Verification

### Health Check Endpoint

```bash
curl http://localhost:80/health
```

**Expected Response** (200 OK):
```json
{
  "status": "healthy",
  "version": "0.3.0",
  "environment": "production",
  "services": {
    "job_manager": {"available": true, "status": "connected"},
    "batch_processing": {"available": true},
    "pdf_generation": {"available": true}
  },
  "auth_enabled": true
}
```

### Additional Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Basic health check |
| `GET /metrics` | Prometheus-format metrics |
| `GET /metrics/health-score` | System health score (0-100) |
| `GET /docs` | Swagger UI |

### Docker Health Check

The container includes a built-in health check:
- Interval: 30s
- Timeout: 10s
- Start period: 5s
- Retries: 3

Check container health:
```bash
docker inspect --format='{{.State.Health.Status}}' downloader
```

## Security Checklist

Before going to production:

- [ ] Set `ENVIRONMENT=production`
- [ ] Set `DOWNLOADER_KEY` to a strong secret (32+ characters)
- [ ] Restrict `CORS_ALLOWED_ORIGINS` to your domains
- [ ] Verify SSRF protection is enabled (default: true)
- [ ] Verify rate limiting is enabled (default: true)
- [ ] Use HTTPS via reverse proxy (Nginx, Caddy, etc.)
- [ ] Review resource limits in docker-compose.prod.yml

### Authentication

When `DOWNLOADER_KEY` is set, all API requests require authentication:

```bash
# Using Bearer token
curl -H "Authorization: Bearer your-api-key" http://localhost:80/download?url=...

# Using X-API-Key header
curl -H "X-API-Key: your-api-key" http://localhost:80/download?url=...
```

### SSRF Protection

Enabled by default. Blocks:
- Private IP ranges (10.x, 172.16-31.x, 192.168.x)
- Localhost (127.0.0.1)
- Cloud metadata endpoints (169.254.169.254)

### Rate Limiting

Default limits (configurable):
- General endpoints: 100/minute
- Download endpoints: 60/minute
- Batch creation: 20/minute
- Status/metrics: 200/minute

## Reverse Proxy Setup

For HTTPS, place behind a reverse proxy. Example Nginx configuration:

```nginx
server {
    listen 443 ssl http2;
    server_name api.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/api.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://localhost:80;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # For large file downloads
        proxy_read_timeout 300s;
        proxy_buffering off;
    }
}
```

## Troubleshooting

### Container Won't Start

```bash
# Check logs
docker-compose logs app

# Common issues:
# - Port 80 already in use: Change port mapping in docker-compose
# - Playwright browser missing: Rebuild image
# - Permission denied: Check file permissions
```

### Health Check Failing

```bash
# Test manually inside container
docker exec -it downloader curl http://localhost:80/health

# Check if Redis is reachable (if configured)
docker exec -it downloader redis-cli -h redis ping
```

### High Memory Usage

PDF generation is memory-intensive. Reduce:
```bash
PDF_CONCURRENCY=2
PDF_POOL_SIZE=1
BATCH_CONCURRENCY=8
```

### Connection Errors

Check HTTP client settings:
```bash
HTTP_MAX_CONNECTIONS=200
HTTP_REQUEST_TIMEOUT=30
HTTP_MAX_REDIRECTS=10
```

### Log Analysis

Enable JSON logs for structured logging:
```bash
LOG_JSON_LOGS=true
```

View logs:
```bash
docker-compose logs -f app | jq '.'
```

## Monitoring

See [Monitoring Guide](./monitoring.md) for:
- Prometheus metrics collection
- Grafana dashboard setup
- Alerting configuration

## Related Documentation

- [Configuration Guide](./configuration.md) - All configuration options
- [API Reference](../api/api-reference.md) - API endpoints and schemas
- [Monitoring Guide](./monitoring.md) - Observability setup
