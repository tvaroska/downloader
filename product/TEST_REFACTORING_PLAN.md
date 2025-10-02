# Test Suite Refactoring Plan - R2

## 📊 Current State Analysis

### Test Statistics
- **Total Tests**: 149 tests
- **Test Files**: 18 files
- **Current Runtime**: >30s (BLOCKING - needs to be <10s for CI/CD)
- **Current Issues**:
  - No test markers (smoke/integration/e2e separation)
  - Playwright tests run by default (slow, ~15-20s)
  - Redis integration tests require external service
  - No timeout protection (tests can hang indefinitely)
  - Fixtures scattered across conftest.py
  - No coverage reporting
  - No test documentation

### Current Test Structure
```
tests/
├── conftest.py                    # 164 lines - all fixtures
├── api/                           # API integration tests
│   ├── test_batch_auth.py
│   ├── test_batch_processing.py   # Heavy Playwright usage
│   ├── test_batch_validation.py
│   ├── test_content_conversion.py
│   ├── test_download.py           # PDF/Playwright tests
│   ├── test_health.py
│   └── test_jobs.py
├── test_auth.py                   # Unit tests
├── test_http_client.py            # Unit tests
├── test_job_manager.py            # Requires Redis
├── test_pdf_generator_*.py        # Playwright heavy (3 files)
├── test_rate_limiting.py          # Config tests
├── test_ssrf_protection.py        # Security tests
└── test_validation.py             # Unit tests
```

---

## 🎯 3-Tier Test Strategy

### **Tier 1: SMOKE Tests** (Target: <3s)
**Purpose**: Fast sanity checks for every commit
**Run When**: Every commit, pre-push, CI on every PR
**Scope**: Core functionality without external dependencies

**Coverage**:
- ✅ Server starts successfully
- ✅ Health endpoint responds
- ✅ Basic text download (mocked HTTP)
- ✅ Configuration loads correctly
- ✅ Authentication logic works
- ✅ SSRF validation works
- ✅ Rate limiting configuration
- ✅ URL validation
- ✅ Content conversion (text/HTML/markdown)

**Exclusions**:
- ❌ No Playwright/PDF generation
- ❌ No Redis integration
- ❌ No real HTTP requests
- ❌ No batch processing

**Expected Tests**: ~40-50 tests
**Marker**: `@pytest.mark.smoke`

---

### **Tier 2: INTEGRATION Tests** (Target: <15s)
**Purpose**: Test with Playwright and batch processing
**Run When**: Before merge, nightly CI, manual trigger
**Scope**: External dependencies (Playwright, Redis optional)

**Coverage**:
- ✅ PDF generation (Playwright)
- ✅ Batch processing with mocked Redis
- ✅ Browser pool management
- ✅ Concurrent PDF requests
- ✅ Complex content conversion
- ✅ Job management logic
- ✅ Metrics collection

**Expected Tests**: ~60-80 tests
**Marker**: `@pytest.mark.integration`

---

### **Tier 3: E2E Tests** (Target: <60s)
**Purpose**: Full system test with Docker + Redis
**Run When**: Before release, nightly build, manual
**Scope**: Complete production-like environment

**Coverage**:
- ✅ Docker container builds and runs
- ✅ Redis integration (real instance)
- ✅ All examples execute successfully
- ✅ Multi-format batch processing
- ✅ Real PDF generation
- ✅ Performance benchmarks
- ✅ Resource cleanup verification

**Expected Tests**: ~20-30 tests + example validation
**Marker**: `@pytest.mark.e2e`

---

## 📁 Proposed Directory Structure

```
tests/
├── README.md                      # Test documentation
├── conftest.py                    # Root-level shared config
├── pytest.ini                     # Pytest configuration (moved from pyproject.toml)
├── .coveragerc                    # Coverage configuration
│
├── fixtures/                      # Organized fixture modules
│   ├── __init__.py
│   ├── api_fixtures.py           # API client, auth headers
│   ├── mock_fixtures.py          # HTTP, Redis, Playwright mocks
│   ├── data_fixtures.py          # Sample content, metadata
│   └── env_fixtures.py           # Environment configuration
│
├── smoke/                         # Tier 1: Smoke tests
│   ├── __init__.py
│   ├── test_server_startup.py    # Server starts, health check
│   ├── test_text_download.py     # Basic download (mocked)
│   ├── test_validation.py        # URL/SSRF validation
│   ├── test_auth.py              # Auth logic
│   ├── test_config.py            # Configuration
│   └── test_rate_limiting.py     # Rate limit config
│
├── integration/                   # Tier 2: Integration tests
│   ├── __init__.py
│   ├── test_pdf_generation.py    # Playwright PDF tests
│   ├── test_batch_processing.py  # Batch with mocked Redis
│   ├── test_content_conversion.py # All format conversions
│   ├── test_browser_pool.py      # Browser management
│   └── test_job_manager.py       # Job logic (mocked Redis)
│
├── e2e/                           # Tier 3: End-to-end tests
│   ├── __init__.py
│   ├── conftest.py               # E2E-specific fixtures
│   ├── docker-compose.yml        # Redis + app
│   ├── test_full_stack.py        # Complete workflow tests
│   ├── test_examples.py          # Run all examples/
│   └── test_performance.py       # Benchmarks
│
└── unit/                          # Pure unit tests (no markers needed)
    ├── __init__.py
    ├── test_http_client.py
    ├── test_content_converter.py
    └── test_utils.py
```

---

## 🏗️ Implementation Plan

### **Phase 1: Setup & Configuration** (1 hour)

**1.1 Install Dependencies**
```bash
uv add pytest-timeout pytest-xdist pytest-benchmark --dev
```

**1.2 Create pytest.ini**
```ini
[pytest]
minversion = 8.0
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
asyncio_mode = auto

# Markers
markers =
    smoke: Fast sanity checks (<3s total)
    integration: Tests with Playwright/Redis (15s)
    e2e: Full system tests with Docker (60s)
    slow: Known slow tests
    requires_redis: Tests requiring Redis
    requires_playwright: Tests requiring Playwright

# Timeout defaults
timeout = 30
timeout_method = thread

# Output
addopts =
    -v
    --strict-markers
    --tb=short
    --show-capture=no

# Coverage
[coverage:run]
source = src/downloader
omit =
    */tests/*
    */__init__.py
    */conftest.py

[coverage:report]
precision = 2
show_missing = True
skip_covered = False
```

**1.3 Create .coveragerc**
```ini
[run]
source = src/downloader
branch = True
omit =
    */tests/*
    */__init__.py
    */conftest.py
    */examples/*

[report]
precision = 2
show_missing = True
skip_covered = False
exclude_lines =
    pragma: no cover
    def __repr__
    raise AssertionError
    raise NotImplementedError
    if __name__ == .__main__.:
    if TYPE_CHECKING:
    @abstractmethod
```

---

### **Phase 2: Reorganize Fixtures** (1.5 hours)

**2.1 Create `tests/fixtures/__init__.py`**
- Import all fixtures for easy access
- Auto-discover fixture modules

**2.2 Create `tests/fixtures/api_fixtures.py`**
```python
"""API client and authentication fixtures."""
import pytest
from fastapi.testclient import TestClient
from src.downloader.main import app

@pytest.fixture
def api_client():
    """Lightweight test client (smoke tests)."""
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client

@pytest.fixture
def auth_headers():
    """Valid authentication headers."""
    return {"Authorization": "Bearer test-key"}

@pytest.fixture
def api_key_headers():
    """Valid API key headers."""
    return {"X-API-Key": "test-key"}
```

**2.3 Create `tests/fixtures/mock_fixtures.py`**
- Move all mock fixtures (HTTP, Redis, Playwright)
- Organize by mock type

**2.4 Create `tests/fixtures/data_fixtures.py`**
- Sample HTML content
- Metadata objects
- Batch requests

**2.5 Create `tests/fixtures/env_fixtures.py`**
- Environment variable fixtures
- Configuration overrides

**2.6 Update `tests/conftest.py`**
```python
"""Root conftest - imports all fixture modules."""
pytest_plugins = [
    "tests.fixtures.api_fixtures",
    "tests.fixtures.mock_fixtures",
    "tests.fixtures.data_fixtures",
    "tests.fixtures.env_fixtures",
]
```

---

### **Phase 3: Migrate Tests to Tiers** (2 hours)

**3.1 Tier 1: Smoke Tests**

Create `tests/smoke/test_server_startup.py`:
```python
"""Smoke tests for server startup and basic functionality."""
import pytest

@pytest.mark.smoke
def test_server_starts(api_client):
    """Test server starts and responds."""
    response = api_client.get("/health")
    assert response.status_code == 200

@pytest.mark.smoke
def test_health_endpoint_structure(api_client):
    """Test health endpoint returns expected structure."""
    response = api_client.get("/health")
    data = response.json()
    assert "status" in data
    assert "version" in data
    assert data["status"] == "healthy"
```

Move tests:
- `test_auth.py` → `smoke/test_auth.py` (add @pytest.mark.smoke)
- `test_validation.py` → `smoke/test_validation.py`
- `test_ssrf_protection.py` → `smoke/test_validation.py` (merge)
- `test_rate_limiting.py` → `smoke/test_rate_limiting.py`
- `api/test_health.py` → `smoke/test_server_startup.py` (merge)

**3.2 Tier 2: Integration Tests**

Move tests:
- `test_pdf_generator_*.py` → `integration/test_pdf_generation.py`
- `api/test_batch_processing.py` → `integration/test_batch_processing.py`
- `api/test_download.py` (PDF tests only) → `integration/test_pdf_generation.py`
- `api/test_content_conversion.py` → Keep in smoke (fast)

**3.3 Tier 3: E2E Tests**

Create new:
- `e2e/test_examples.py` - Run all examples/*.py
- `e2e/test_full_stack.py` - End-to-end workflows
- `e2e/docker-compose.yml` - Redis + app

---

### **Phase 4: Add Test Markers** (30 min)

Add markers to all existing tests:
```python
# Smoke test example
@pytest.mark.smoke
def test_basic_functionality():
    pass

# Integration test example
@pytest.mark.integration
@pytest.mark.requires_playwright
@pytest.mark.timeout(10)
def test_pdf_generation():
    pass

# E2E test example
@pytest.mark.e2e
@pytest.mark.requires_redis
@pytest.mark.timeout(30)
def test_full_workflow():
    pass
```

---

### **Phase 5: Create Test Documentation** (1 hour)

**5.1 Create `tests/README.md`**

```markdown
# Test Suite Documentation

## Overview
Multi-tier test strategy with 3 levels: Smoke, Integration, E2E

## Running Tests

### Smoke Tests (Fast - <3s)
```bash
pytest -m smoke              # All smoke tests
pytest tests/smoke/          # By directory
```

### Integration Tests (Medium - <15s)
```bash
pytest -m integration        # All integration tests
pytest tests/integration/    # By directory
```

### E2E Tests (Slow - <60s)
```bash
pytest -m e2e                # Requires Docker
cd tests/e2e && docker-compose up -d
pytest tests/e2e/
```

### All Tests
```bash
pytest                       # Run everything
pytest --cov                 # With coverage
```

## Test Markers
- `smoke`: Fast sanity checks
- `integration`: Playwright/batch processing
- `e2e`: Full system with Docker
- `slow`: Known slow tests (>5s)
- `requires_redis`: Needs Redis instance
- `requires_playwright`: Needs Playwright

## Writing Tests
See CONTRIBUTING.md for test writing guidelines
```

---

### **Phase 6: Create Helper Scripts** (30 min)

**6.1 Create `scripts/test_smoke.sh`**
```bash
#!/bin/bash
set -e
echo "Running smoke tests..."
uv run pytest -m smoke --tb=short -q
echo "✅ Smoke tests passed!"
```

**6.2 Create `scripts/test_all.sh`**
```bash
#!/bin/bash
set -e
echo "Running all test tiers..."
pytest -m smoke --tb=short -q
pytest -m integration --tb=short -q
pytest -m e2e --tb=short -q
echo "✅ All tests passed!"
```

**6.3 Create `scripts/test_coverage.sh`**
```bash
#!/bin/bash
uv run pytest -m "smoke or integration" \
    --cov=src/downloader \
    --cov-report=html \
    --cov-report=term-missing \
    --cov-report=xml
echo "Coverage report: htmlcov/index.html"
```

---

### **Phase 7: E2E Docker Setup** (1 hour)

**7.1 Create `tests/e2e/docker-compose.yml`**
```yaml
version: '3.8'

services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  downloader:
    build:
      context: ../..
      dockerfile: Dockerfile
    ports:
      - "8000:8000"
    environment:
      - REDIS_URI=redis://redis:6379
      - LOG_LEVEL=INFO
    depends_on:
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 3
```

**7.2 Create `tests/e2e/test_examples.py`**
```python
"""E2E tests - run all examples."""
import pytest
import subprocess

@pytest.mark.e2e
@pytest.mark.timeout(30)
def test_basic_usage_example():
    """Test basic_usage.py runs successfully."""
    result = subprocess.run(
        ["python", "examples/basic_usage.py"],
        capture_output=True,
        timeout=30
    )
    assert result.returncode == 0
```

---

## ⏱️ Timeline & Effort Estimate

| Phase | Task | Effort | Dependencies |
|-------|------|--------|--------------|
| 1 | Setup & Configuration | 1h | None |
| 2 | Reorganize Fixtures | 1.5h | Phase 1 |
| 3 | Migrate Tests to Tiers | 2h | Phase 2 |
| 4 | Add Test Markers | 30min | Phase 3 |
| 5 | Test Documentation | 1h | Phase 4 |
| 6 | Helper Scripts | 30min | Phase 5 |
| 7 | E2E Docker Setup | 1h | Phase 6 |
| **TOTAL** | **7.5 hours** | | |

**Original Estimate**: 4-6 hours
**Revised Estimate**: 6-8 hours (more comprehensive)

---

## 📊 Expected Outcomes

### Performance Improvements
- **Smoke Tests**: 3s (currently: N/A)
- **Integration Tests**: 15s (currently: 30s+)
- **E2E Tests**: 60s (currently: N/A)
- **CI/CD**: Run smoke on every commit (<3s)

### Code Quality
- **Coverage**: >80% (currently: unknown)
- **Test Organization**: 3 clear tiers
- **Maintainability**: Organized fixtures
- **Documentation**: Complete test guide

### Developer Experience
- **Fast Feedback**: <3s smoke tests
- **Selective Running**: Run only needed tiers
- **Clear Markers**: Know what each test does
- **Easy Debugging**: Organized structure

---

## 🎯 Success Criteria

- [ ] Smoke tests run in <3 seconds
- [ ] All tests have appropriate markers
- [ ] Fixtures organized into modules
- [ ] Coverage >80% for smoke+integration
- [ ] Tests documented in tests/README.md
- [ ] E2E tests run in Docker
- [ ] All 5 examples validated in E2E
- [ ] CI/CD can run smoke tests on every commit
- [ ] No hanging tests (all have timeouts)
- [ ] Test structure follows project structure

---

## 🚀 Next Steps

1. **Review this plan** with team
2. **Approve timeline** (6-8 hours)
3. **Execute phases** 1-7 sequentially
4. **Update R2 in roadmap** when complete
5. **Document in PROGRESS.md**

---

## 📚 References

- Pytest markers: https://docs.pytest.org/en/latest/how-to/mark.html
- Pytest timeout: https://pypi.org/project/pytest-timeout/
- Coverage.py: https://coverage.readthedocs.io/
- Docker Compose: https://docs.docker.com/compose/
