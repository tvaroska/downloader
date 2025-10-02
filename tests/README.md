# Test Suite Documentation

This test suite uses a **3-tier testing strategy** to optimize test execution speed and clarity.

## Test Tiers

### Tier 1: Smoke Tests (`tests/smoke/`)
**Purpose:** Fast validation of core functionality
**Execution Time:** < 30 seconds
**When to Run:** On every commit, in CI/CD pipeline

**Characteristics:**
- No external dependencies (no Redis, no Playwright)
- Pure unit tests and basic integration tests
- Mock all I/O operations
- Test critical paths only

**What's Included:**
- Authentication (`test_auth.py`)
- URL validation and SSRF protection (`test_validation.py`)
- Rate limiting configuration (`test_rate_limiting.py`)
- Server startup and health (`test_server_startup.py`)
- Content conversion utilities (`api/test_content_conversion.py`)

**How to Run:**
```bash
# Run all smoke tests
pytest -m smoke

# Or use the script
./scripts/test_smoke.sh
```

### Tier 2: Unit Tests (`tests/unit/`)
**Purpose:** Isolated component testing
**Execution Time:** < 1 minute
**When to Run:** During development, before pushing

**Characteristics:**
- Test individual modules in isolation
- Heavy use of mocks and fixtures
- No external dependencies
- Focus on edge cases and error handling

**What's Included:**
- HTTP client (`test_http_client.py`)

**How to Run:**
```bash
# Run all unit tests
pytest -m unit

# Run unit tests with coverage
pytest -m unit --cov=src/downloader --cov-report=term-missing
```

### Tier 3: Integration Tests (`tests/integration/`)
**Purpose:** Test component interactions with external dependencies
**Execution Time:** 2-5 minutes
**When to Run:** Before merging, in nightly builds

**Characteristics:**
- Requires Playwright for PDF generation
- Tests multiple components working together
- May use mocked external services
- Focus on realistic scenarios

**What's Included:**
- PDF generation with Playwright (`test_pdf_generation.py`)
- PDF download functionality (`test_pdf_download.py`)
- Batch processing (`test_batch_processing.py`)

**How to Run:**
```bash
# Run all integration tests
pytest -m integration

# Run integration tests that require Playwright
pytest -m requires_playwright

# Run without Playwright tests
pytest -m "integration and not requires_playwright"
```

### API Tests (`tests/api/`)
**Purpose:** End-to-end API testing
**Execution Time:** 1-2 minutes
**When to Run:** Before deployment

**What's Included:**
- Health endpoint tests
- Job status and management
- Batch job validation and authentication
- Download endpoint (non-PDF tests)

**How to Run:**
```bash
# Run all API tests
pytest tests/api/

# Run specific API test file
pytest tests/api/test_health.py
```

### E2E Tests (`tests/e2e/`)
**Purpose:** Full system testing in production-like environment
**Execution Time:** 5-10 minutes
**When to Run:** Before release, in staging environment

**Characteristics:**
- Uses docker-compose to spin up full environment
- Tests with real Redis, real browser instances
- Validates all 5 example scripts
- Tests complete user workflows

**How to Run:**
```bash
# Run E2E tests (requires Docker)
pytest -m e2e

# Or use docker-compose directly
cd tests/e2e
docker-compose up -d
pytest test_examples.py
docker-compose down
```

## Pytest Markers

All tests are marked with appropriate pytest markers for easy filtering:

| Marker | Description | Location |
|--------|-------------|----------|
| `smoke` | Fast, critical path tests | `tests/smoke/` |
| `unit` | Isolated unit tests | `tests/unit/` |
| `integration` | Integration tests | `tests/integration/` |
| `requires_playwright` | Needs Playwright browser | `tests/integration/` |
| `e2e` | End-to-end tests | `tests/e2e/` |

## Running Tests

### Quick Commands

```bash
# Run ONLY smoke tests (fastest)
pytest -m smoke

# Run smoke + unit tests
pytest -m "smoke or unit"

# Run all tests EXCEPT integration and e2e
pytest -m "not integration and not e2e"

# Run all tests EXCEPT those requiring Playwright
pytest -m "not requires_playwright"

# Run everything
pytest

# Run with coverage report
pytest --cov=src/downloader --cov-report=html
```

### Using Test Scripts

```bash
# Run smoke tests only
./scripts/test_smoke.sh

# Run all tiers sequentially (smoke -> unit -> integration -> api)
./scripts/test_all.sh

# Generate coverage report
./scripts/test_coverage.sh
```

## Test Organization

```
tests/
├── smoke/              # Tier 1: Smoke tests
│   ├── __init__.py
│   ├── test_auth.py
│   ├── test_validation.py
│   ├── test_rate_limiting.py
│   └── test_server_startup.py
│
├── unit/               # Tier 2: Unit tests
│   ├── __init__.py
│   └── test_http_client.py
│
├── integration/        # Tier 3: Integration tests
│   ├── __init__.py
│   ├── test_pdf_generation.py
│   ├── test_pdf_download.py
│   └── test_batch_processing.py
│
├── api/                # API endpoint tests
│   ├── __init__.py
│   ├── test_health.py
│   ├── test_jobs.py
│   ├── test_batch_auth.py
│   ├── test_batch_validation.py
│   ├── test_content_conversion.py (marked as smoke)
│   └── test_download.py
│
├── e2e/                # End-to-end tests
│   ├── __init__.py
│   ├── conftest.py
│   ├── docker-compose.yml
│   └── test_examples.py
│
├── fixtures/           # Shared test fixtures
│   ├── __init__.py
│   ├── api_fixtures.py
│   ├── data_fixtures.py
│   ├── env_fixtures.py
│   └── mock_fixtures.py
│
├── conftest.py         # Root pytest configuration
├── pytest.ini          # Pytest settings
└── README.md           # This file
```

## Fixtures

All shared fixtures are located in `tests/fixtures/`:

- **api_fixtures.py**: Mock HTTP clients, job managers, browser pools
- **data_fixtures.py**: Sample HTML content, metadata, test data
- **env_fixtures.py**: Environment variable fixtures (auth, Redis)
- **mock_fixtures.py**: Mock Playwright, PDF generators

Import fixtures in your tests:
```python
from tests.fixtures.api_fixtures import mock_http_client
from tests.fixtures.env_fixtures import env_with_redis
```

## CI/CD Integration

### GitHub Actions Example

```yaml
# .github/workflows/test.yml
name: Tests

on: [push, pull_request]

jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Run smoke tests
        run: |
          pip install -e .
          pytest -m smoke

  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Run unit tests
        run: |
          pip install -e .
          pytest -m unit

  integration:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Install Playwright
        run: playwright install chromium
      - name: Run integration tests
        run: |
          pip install -e .
          pytest -m integration
```

## Writing New Tests

### Adding a Smoke Test

1. Create test file in `tests/smoke/`
2. Add `@pytest.mark.smoke` decorator to test class or function
3. Mock all external dependencies
4. Keep execution time < 5 seconds per test

```python
import pytest

@pytest.mark.smoke
class TestMyFeature:
    def test_basic_functionality(self):
        # Test implementation
        pass
```

### Adding a Unit Test

1. Create test file in `tests/unit/`
2. Add `@pytest.mark.unit` decorator
3. Test single component in isolation
4. Use fixtures from `tests/fixtures/`

```python
import pytest

@pytest.mark.unit
class TestMyModule:
    def test_edge_case(self, mock_dependency):
        # Test implementation
        pass
```

### Adding an Integration Test

1. Create test file in `tests/integration/`
2. Add `@pytest.mark.integration` decorator
3. Add `@pytest.mark.requires_playwright` if needed
4. Test realistic component interactions

```python
import pytest

@pytest.mark.integration
@pytest.mark.requires_playwright
class TestMyIntegration:
    async def test_full_workflow(self, mock_browser_pool):
        # Test implementation
        pass
```

## Troubleshooting

### Common Issues

**Issue: "No module named 'playwright'"**
```bash
# Install playwright
pip install playwright
playwright install chromium
```

**Issue: "Redis connection failed"**
```bash
# Use env_with_redis fixture or mock Redis
def test_with_redis(env_with_redis, mock_job_manager):
    # Test will use mocked Redis
    pass
```

**Issue: Tests are slow**
```bash
# Run only smoke tests for quick feedback
pytest -m smoke

# Run tests in parallel (requires pytest-xdist)
pip install pytest-xdist
pytest -n auto
```

## Coverage

Generate coverage reports:

```bash
# Terminal report
pytest --cov=src/downloader --cov-report=term-missing

# HTML report (opens in browser)
pytest --cov=src/downloader --cov-report=html
open htmlcov/index.html

# Use the coverage script
./scripts/test_coverage.sh
```

## Best Practices

1. **Keep smoke tests fast** - No external dependencies, no slow operations
2. **Use appropriate markers** - Mark tests with `@pytest.mark.smoke`, `@pytest.mark.unit`, etc.
3. **Leverage fixtures** - Reuse fixtures from `tests/fixtures/`
4. **Test one thing** - Each test should validate one specific behavior
5. **Clear test names** - Use descriptive names: `test_download_with_invalid_url_returns_400`
6. **Mock external services** - Don't rely on external APIs in tests
7. **Clean up resources** - Use fixtures with `yield` for proper cleanup
8. **Document complex tests** - Add docstrings explaining what's being tested

## Test Data

Sample test data is available in `tests/fixtures/data_fixtures.py`:
- `sample_html_content`: Basic HTML for testing
- `sample_metadata`: Standard metadata dictionary
- `sample_json_response`: Example JSON API response

## Performance

Target execution times:
- Smoke tests: < 30 seconds total
- Unit tests: < 1 minute total
- Integration tests: < 5 minutes total
- API tests: < 2 minutes total
- E2E tests: < 10 minutes total

**Total test suite**: < 20 minutes (all tiers)

## Questions?

For questions or issues with the test suite, please:
1. Check this README
2. Review existing test files for examples
3. Check `tests/conftest.py` for available fixtures
4. Open an issue with the `testing` label
