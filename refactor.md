# Playwright Worker-Based Architecture Refactor

## Current Playwright Architecture Analysis

### Current Constraints
- **Fixed pool size**: 2-3 browsers per instance (hardcoded)
- **Memory usage**: Each browser ~100-200MB, contexts ~10-50MB each
- **CPU intensive**: PDF generation blocks event loop during rendering
- **Single point of failure**: If browser pool crashes, entire service affected
- **Resource competition**: PDF and text extraction compete for same browser pool

### Current Concurrency Limits
- **PDF Generation**: `PDF_SEMAPHORE = asyncio.Semaphore(5)` → **5 concurrent PDF generations**
- **Batch Downloads**: `BATCH_SEMAPHORE = asyncio.Semaphore(20)` → **20 concurrent batch downloads**
- **Browser Pool Size**: `pool_size=2` (Docker) or `pool_size=3` (default)

## Proposed Worker-Based Architecture

### Architecture Overview

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Main API      │    │   Load Balancer  │    │  Playwright     │
│   Server        │───▶│   / Queue        │───▶│  Worker 1       │
│                 │    │                  │    │                 │
│ - HTTP endpoints│    │ - Task routing   │    │ - Browser pool  │
│ - Auth/validation│    │ - Health checks  │    │ - PDF generation│
│ - Content proc. │    │ - Failover       │    │ - Text extraction│
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                │                          
                                │               ┌─────────────────┐
                                └──────────────▶│  Playwright     │
                                                │  Worker 2       │
                                                │                 │
                                                │ - Browser pool  │
                                                │ - Independent   │
                                                └─────────────────┘
```

### Interface Design

**Worker Service API:**
```python
# New service: playwright-worker
class PlaywrightWorkerService:
    async def generate_pdf(self, url: str, options: dict) -> bytes
    async def extract_content(self, url: str, format: str) -> str
    async def health_check(self) -> dict
```

**Main API Integration:**
```python
# Modified api.py
class PlaywrightClient:
    def __init__(self, worker_urls: list[str]):
        self.workers = [HTTPClient(url) for url in worker_urls]
        self.load_balancer = RoundRobinBalancer(self.workers)
    
    async def generate_pdf(self, url: str, options: dict) -> bytes:
        worker = await self.load_balancer.get_healthy_worker()
        return await worker.post("/pdf", {"url": url, "options": options})
```

## Scaling and Performance Benefits

### Capacity Improvements

**Current:** 5 concurrent PDF generations + 2-3 browser pool
**With Workers:** N workers × browsers per worker

**Example Scaling:**
- **3 workers**: 3 × 5 browsers = **15 concurrent PDF generations** (3x improvement)
- **5 workers**: 5 × 5 browsers = **25 concurrent PDF generations** (5x improvement)
- **Auto-scaling**: Add workers during peak load, remove during low usage

### Performance Benefits

**Resource Isolation:**
- PDF failures don't affect main API
- Memory leaks contained per worker
- CPU-intensive rendering doesn't block main event loop

**Specialized Optimization:**
- Workers can use different browser configurations
- Dedicated workers for PDF vs text extraction
- Workers can run on GPU-optimized instances

**Horizontal Scaling:**
- Scale PDF workers independently from API servers
- Deploy workers closer to target content geographically
- Load balance based on worker health/performance

## Implementation Challenges and Solutions

### Challenge 1: Network Latency
**Problem:** HTTP calls to workers add latency vs in-process calls
**Solutions:**
- Keep workers on same network/cluster (< 1ms latency)
- Use HTTP/2 connection pooling
- Batch multiple requests when possible

### Challenge 2: Error Handling & Retries
**Problem:** Network failures, worker crashes need robust handling
**Solutions:**
- Circuit breaker pattern for failing workers
- Automatic failover to healthy workers
- Exponential backoff retry with jitter
- Health check endpoints (`/health`)

### Challenge 3: State Management
**Problem:** Current global PDF generator state needs distribution
**Solutions:**
```python
# Replace global instance with worker pool client
class WorkerPoolClient:
    def __init__(self):
        self.workers = self._discover_workers()
        self.health_checker = HealthChecker(self.workers)
    
    async def generate_pdf(self, url: str, options: dict) -> bytes:
        for attempt in range(3):
            worker = await self._get_healthy_worker()
            try:
                return await worker.generate_pdf(url, options)
            except WorkerError:
                await self._mark_unhealthy(worker)
                continue
```

### Challenge 4: Deployment Complexity
**Problem:** Multiple services to deploy and coordinate
**Solutions:**
- Docker Compose for local development
- Kubernetes for production with auto-scaling
- Shared configuration via environment variables
- Service discovery (Consul/etcd or k8s services)

### Challenge 5: Resource Management
**Problem:** Worker resource allocation and monitoring
**Solutions:**
- Worker-level metrics (CPU, memory, active browsers)
- Automatic worker restart on memory leaks
- Queue-based load distribution during spikes

## Implementation Roadmap

### Phase 1: Service Extraction
**Goal:** Extract Playwright logic into separate service

**Tasks:**
1. Create new `playwright-worker` service
2. Move `PlaywrightPDFGenerator` and `BrowserPool` classes
3. Add HTTP API endpoints for PDF generation and content extraction
4. Create `PlaywrightClient` in main API to communicate with workers
5. Update main API to use client instead of direct calls

**Files to modify:**
- Create new service directory: `services/playwright-worker/`
- Move logic from `src/downloader/pdf_generator.py`
- Update `src/downloader/api.py` to use client
- Add Docker configuration for worker service

### Phase 2: Load Balancing and Health Checks
**Goal:** Add resilience and worker management

**Tasks:**
1. Implement worker health checking
2. Add load balancing strategies (round-robin, least-busy)
3. Circuit breaker pattern for failing workers
4. Retry logic with exponential backoff
5. Worker discovery mechanism

### Phase 3: Auto-scaling and Advanced Routing
**Goal:** Production-ready scaling features

**Tasks:**
1. Queue-based work distribution using Redis
2. Auto-scaling based on queue depth
3. Worker specialization (PDF vs text extraction)
4. Performance monitoring and metrics
5. Kubernetes deployment configuration

## Recommendation

### Benefits Summary
✅ **3-5x scaling**: From 5 → 15-25 concurrent PDF generations  
✅ **Resource isolation**: PDF failures don't crash main API  
✅ **Horizontal scaling**: Add workers during peak load  
✅ **Specialized optimization**: GPU instances, geo-distribution  

### Implementation Priority
1. **Phase 1**: Extract Playwright logic into separate service
2. **Phase 2**: Add load balancing and health checks  
3. **Phase 3**: Auto-scaling and advanced routing

### Key Design Decisions
- **Queue-based**: Use Redis/RabbitMQ for work distribution during high load
- **Stateless workers**: Each worker maintains its own browser pool
- **Graceful degradation**: Fall back to fewer workers if some fail
- **Unified interface**: Keep same API contract, change implementation

This architecture would significantly improve the system's ability to handle concurrent PDF generation while maintaining the clean interface already established.

## Current Interface Status

The downloader already implements an ideal unified interface:

**Unified Components:**
- `PlaywrightPDFGenerator` class (`pdf_generator.py:128-300`)
- Global shared instance with `get_pdf_generator()` context manager (`pdf_generator.py:307-336`)
- Common functions: `generate_pdf_from_url()` and `convert_content_with_playwright_fallback()`

**Both APIs use identical paths:**
- Online: `api.py:1378` → `generate_pdf_from_url()` → shared generator
- Batch: `api.py:722` → `generate_pdf_from_url()` → shared generator
- Fallback: Both use `convert_content_with_playwright_fallback()` → shared generator pool

This unified interface makes the worker-based refactor straightforward - we only need to change the implementation behind these functions, not the API contract.