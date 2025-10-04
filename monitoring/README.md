# Production Monitoring Setup

This directory contains a complete monitoring stack for the REST API Downloader service, including Prometheus metrics collection and Grafana dashboards.

## 🚀 Quick Start

### 1. Start the Monitoring Stack

```bash
# From the monitoring directory
cd monitoring
docker-compose -f docker-compose.monitoring.yml up -d
```

### 2. Access Dashboards

- **Grafana Dashboard**: http://localhost:3000
  - Username: `admin`
  - Password: `admin123`
- **Prometheus**: http://localhost:9090
- **Redis Exporter** (if using Redis): http://localhost:9121

### 3. Start the Downloader Service

Ensure your downloader service is running and accessible. The metrics will be automatically collected from `/metrics` endpoint.

```bash
# Start your downloader service
cd ..
uv run uvicorn src.downloader.main:app --host 0.0.0.0 --port 8000
```

## 📊 Available Metrics

### Core Application Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `downloader_requests_total` | Counter | Total number of requests by endpoint |
| `downloader_errors_total` | Counter | Total number of errors by endpoint |
| `downloader_response_time_seconds` | Histogram | Response time distribution |
| `downloader_uptime_seconds` | Gauge | Service uptime in seconds |

### System Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `downloader_gauge{name="pdf_concurrency_utilization"}` | Gauge | PDF concurrency utilization % |
| `downloader_gauge{name="batch_concurrency_utilization"}` | Gauge | Batch concurrency utilization % |
| `downloader_gauge{name="redis_status"}` | Gauge | Redis connection status (0/1) |
| `downloader_gauge{name="http_client_status"}` | Gauge | HTTP client status (0/1) |

### Connection Pool Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `downloader_gauge{name="redis_available_connections"}` | Gauge | Available Redis connections |
| `downloader_gauge{name="redis_in_use_connections"}` | Gauge | In-use Redis connections |

## 🎯 Dashboard Features

The Grafana dashboard provides:

1. **System Health Score** - Overall health percentage
2. **Request Rate** - Real-time request volume
3. **Error Rate** - Error percentage monitoring
4. **Response Time Distribution** - P50, P90, P95, P99 percentiles
5. **Concurrency Utilization** - PDF and batch processing utilization
6. **Connection Pool Status** - Redis and HTTP client health
7. **Status Code Distribution** - HTTP status code breakdown
8. **Endpoint Performance Table** - Per-endpoint metrics

## 🔍 Custom Queries

### Useful Prometheus Queries

```promql
# Request rate per endpoint
rate(downloader_requests_total[5m])

# Error rate percentage
rate(downloader_errors_total[5m]) / rate(downloader_requests_total[5m]) * 100

# 95th percentile response time
histogram_quantile(0.95, rate(downloader_response_time_seconds_bucket[5m]))

# High error rate endpoints
topk(5, rate(downloader_errors_total[5m]))

# Slow endpoints (P95 > 2 seconds)
histogram_quantile(0.95, rate(downloader_response_time_seconds_bucket[5m])) > 2
```

## 🚨 Alerting Rules

### Recommended Alerts

Create these alerting rules in `prometheus.yml`:

```yaml
# alerting_rules.yml
groups:
  - name: downloader_alerts
    rules:
      - alert: HighErrorRate
        expr: rate(downloader_errors_total[5m]) / rate(downloader_requests_total[5m]) * 100 > 5
        for: 2m
        annotations:
          summary: "High error rate detected"
          description: "Error rate is {{ $value }}% over the last 5 minutes"

      - alert: SlowResponseTime
        expr: histogram_quantile(0.95, rate(downloader_response_time_seconds_bucket[5m])) > 3
        for: 5m
        annotations:
          summary: "Slow response time detected"
          description: "95th percentile response time is {{ $value }}s"

      - alert: ServiceDown
        expr: up{job="downloader"} == 0
        for: 1m
        annotations:
          summary: "Downloader service is down"
          description: "The downloader service has been down for more than 1 minute"

      - alert: HighConcurrencyUtilization
        expr: downloader_gauge{name=~".*_concurrency_utilization"} > 90
        for: 5m
        annotations:
          summary: "High concurrency utilization"
          description: "{{ $labels.name }} utilization is {{ $value }}%"
```

## 🛠️ Configuration

### Environment Variables

The monitoring stack supports these environment variables:

- `REDIS_URI`: Redis connection string for Redis metrics collection
- `PROMETHEUS_RETENTION`: Data retention period (default: 30d)
- `GRAFANA_ADMIN_PASSWORD`: Grafana admin password (default: admin123)

### Custom Configuration

#### Prometheus Configuration

Edit `prometheus.yml` to adjust:
- Scrape intervals
- Target endpoints
- Alerting rules

#### Grafana Configuration

- Dashboard JSON is in `grafana-dashboard.json`
- Datasource configuration in `grafana/provisioning/datasources/`
- Custom dashboards can be added to `grafana/provisioning/dashboards/`

## 🏗️ Production Deployment

### Scaling Considerations

1. **Prometheus Storage**: Configure appropriate retention and storage
2. **Grafana Performance**: Use external database for large deployments
3. **Network**: Ensure proper network connectivity between services
4. **Security**: Configure authentication and SSL in production

### Docker Compose Override

Create `docker-compose.override.yml` for production settings:

```yaml
version: '3.8'
services:
  prometheus:
    volumes:
      - /path/to/prometheus/data:/prometheus
    environment:
      - PROMETHEUS_RETENTION=90d

  grafana:
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASSWORD}
      - GF_DATABASE_TYPE=postgres
      - GF_DATABASE_HOST=postgres:5432
```

## 🔧 Troubleshooting

### Common Issues

1. **Metrics not appearing**
   - Check service connectivity: `curl http://localhost:8000/metrics`
   - Verify Prometheus targets: http://localhost:9090/targets

2. **Dashboard not loading**
   - Check Grafana logs: `docker logs downloader-grafana`
   - Verify datasource connection in Grafana UI

3. **Redis metrics missing**
   - Ensure Redis is running and accessible
   - Check Redis exporter logs: `docker logs downloader-redis-exporter`

### Verification Commands

```bash
# Check metrics endpoint
curl http://localhost:8000/metrics

# Check live metrics
curl http://localhost:8000/metrics/live

# Check health with metrics
curl http://localhost:8000/metrics/health-score

# Test Prometheus query
curl 'http://localhost:9090/api/v1/query?query=downloader_requests_total'
```

## 📈 Performance Impact

The metrics collection system is designed for minimal performance impact:

- **Memory**: ~10-20MB additional memory usage
- **CPU**: <1% CPU overhead for metrics collection
- **Network**: ~1KB per request for metrics data
- **Storage**: ~100MB per day for metrics retention (depends on request volume)

## 🔄 Maintenance

### Regular Tasks

1. **Review dashboards** weekly for performance trends
2. **Update alerting thresholds** based on baseline performance
3. **Clean up old metrics** data periodically
4. **Backup Grafana dashboards** and configuration

### Metric Retention

Default retention periods:
- **Prometheus**: 30 days
- **Grafana**: Unlimited (dashboards and config)

Adjust based on your monitoring needs and storage constraints.
