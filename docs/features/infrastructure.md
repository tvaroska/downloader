# Infrastructure - Feature History

**Last Updated:** 2026-11-12

## Overview

This document tracks infrastructure and operational improvements for the REST API Downloader service, including graceful shutdown connection draining, split worker architecture, observability, and deployment topology.

---

## Status: ✅ Complete (`v0.7.0`)

Completed: 2026-11-12

---

## Completed Work (`v0.7.0`)

### Graceful Shutdown with Connection Drain (Priority: P2)
- [x] `DrainCoordinator` and `DrainMiddleware` in `src/downloader/drain.py`
- [x] In-flight non-health request tracking; new requests rejected with `HTTP 503` (`service_draining`, `Connection: close`, `Retry-After: 5`) once shutdown begins
- [x] `GET /health` flips to `HTTP 503` with `status: "draining"` and `active_requests` count for Kubernetes readiness eviction
- [x] `JobManager.wait_for_active_jobs()` waits up to `SHUTDOWN_DRAIN_TIMEOUT` (`30s` default) before teardown; `Dockerfile` and Compose files pass `--timeout-graceful-shutdown 35`

### Split Worker Architecture (Priority: P2)
- [x] `DEPLOYMENT_MODE=all|api|worker` (`all` default preserves zero-overhead single-pod stateless behavior)
- [x] `src/downloader/worker_queue.py`: reliable Redis `BLMOVE` batch queue (`downloader:queue:batch` -> `:processing`), remote Playwright/PDF/screenshot RPC queue (`downloader:queue:browser`), and TTL-expiring worker heartbeats (`downloader:worker:heartbeat:*`)
- [x] `src/downloader/worker.py`: dedicated worker runtime with `--healthcheck`, APScheduler execution, graceful shutdown drain, and `just worker` / `just split` recipes
- [x] `api` mode starts APScheduler paused for CRUD-only schedule management while offloading batch jobs and Playwright rendering to workers over Redis
- [x] Updated `docker-compose.yml` (`profiles: ["split"]`), `docker-compose.split.yml`, and `docker-compose.prod.yml`

### OpenTelemetry Observability (Priority: P2)
- [x] Opt-in OpenTelemetry SDK in `src/downloader/telemetry.py` (`OTEL_ENABLED=false` default for zero idle overhead)
- [x] FastAPI, `httpx`, and Redis auto-instrumentation plus custom spans for Playwright render/PDF/screenshot, OCR, webhooks, and worker job execution
- [x] OTLP HTTP exporter (`OTEL_EXPORTER_OTLP_ENDPOINT`) and optional console exporter (`OTEL_CONSOLE_EXPORTER`)
- [x] `trace_id` and `span_id` correlation in `CustomJsonFormatter`, `X-Trace-ID` response header in `middleware.py`, and W3C `traceparent` propagation across Redis worker envelopes

---

## Verification

- `tests/unit/test_v070_ops_drain_worker_otel.py` verifies connection drain timeouts, `503` draining middleware/health responses, `JobManager.wait_for_active_jobs`, `WorkerQueue` `BLMOVE` + browser RPC + heartbeats, `api`-mode batch offloading, worker execution, and OpenTelemetry span recording, log correlation, and W3C propagation.

---

## Related Documentation

- [Production Readiness](production-readiness.md) - Sprint 0 infrastructure work
- [Roadmap](../roadmap.md) - Strategic priorities
