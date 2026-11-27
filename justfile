# List available commands
default:
    @just --list

# Install dependencies
install:
    uv sync --upgrade
    uv run pre-commit install

# Format and lint code
fmt:
    uv run ruff check --fix src/ tests/ examples/
    uv run ruff format src/ tests/ examples/

# Read-only verification gate (Python + typed SDK suites)
check:
    #!/usr/bin/env bash
    set -euo pipefail
    uv lock --check --offline --no-config
    uv run --no-sync ast-grep scan --rule specs/ast.rules.yaml .
    uv run --no-sync ruff check src/ tests/ examples/
    uv run --no-sync ruff format --check src/ tests/ examples/
    uv run --no-sync mypy src/ tests/
    uv run --no-sync pytest -p no:cacheprovider --cov=src/downloader --cov-fail-under=83 -n auto -m "not (integration or e2e or network or requires_redis or requires_playwright)"
    uv run --no-sync pytest -p no:cacheprovider sdks/python/tests -c sdks/python/pyproject.toml
    node --test sdks/javascript/test/client.test.mjs
    if [[ -x sdks/javascript/node_modules/.bin/tsc ]]; then
        sdks/javascript/node_modules/.bin/tsc --noEmit -p sdks/javascript/tsconfig.json
    elif command -v tsc >/dev/null 2>&1; then
        tsc --noEmit -p sdks/javascript/tsconfig.json
    else
        echo "ERROR: tsc not found (run 'npm --prefix sdks/javascript ci')" >&2
        exit 1
    fi
    test -z "$(gofmt -s -l sdks/go)"
    go -C sdks/go vet ./...
    go -C sdks/go test -race ./...

# Run tests
test:
    uv run pytest tests/ -v

# Run development server
dev:
    REDIS_URI="redis://localhost:6379" uv run python -m uvicorn downloader.main:app --host 0.0.0.0 --port 8000 --reload

# Run split worker process locally against Redis
worker:
    REDIS_URI="redis://localhost:6379" DEPLOYMENT_MODE="worker" uv run python -m downloader.worker

# Run split API + Worker + Redis stack via Docker Compose
split:
    docker compose -f docker-compose.yml -f docker-compose.split.yml up -d --build

# Build and push Docker image
docker:
    #!/usr/bin/env bash
    set -e
    VERSION=$(grep '^version = ' pyproject.toml | cut -d'"' -f2)
    echo "Building version: v$VERSION"
    docker build -t us-central1-docker.pkg.dev/sites-470716/containers/downloader:v$VERSION -t us-central1-docker.pkg.dev/sites-470716/containers/downloader:latest .
    docker push us-central1-docker.pkg.dev/sites-470716/containers/downloader:v$VERSION
    docker push us-central1-docker.pkg.dev/sites-470716/containers/downloader:latest

# Clean build artifacts
clean:
    find . -type f -name "*.pyc" -delete
    find . -type d -name "__pycache__" -delete
    rm -rf .pytest_cache htmlcov .coverage

# End-to-end test: build image, run with Redis, test all examples
e2e:
    #!/usr/bin/env bash
    set -e

    # Create logs directory and clean up old logs
    mkdir -p logs
    rm -f logs/e2e-*.log
    echo "🧹 Cleaned up old logs"

    # Clean up any existing containers
    docker compose down -v || true

    # Build the Docker image
    echo "Building Docker image..."
    docker compose build

    # Start services (downloader + redis)
    echo "Starting services..."
    docker compose up -d

    # Wait for services to be ready
    echo "Waiting for services to be ready..."
    sleep 5

    # Check if downloader service is healthy
    max_attempts=30
    attempt=0
    while ! curl -s http://localhost:8000/health > /dev/null; do
        attempt=$((attempt + 1))
        if [ $attempt -ge $max_attempts ]; then
            echo "Service failed to start within timeout"
            docker compose logs > logs/e2e-startup-failure.log
            docker compose down -v
            exit 1
        fi
        echo "Waiting for service... attempt $attempt/$max_attempts"
        sleep 2
    done

    echo "Services are ready!"

    # Run all example scripts
    echo "Running example scripts..."
    export API_URL="http://localhost:8000"
    failed=0

    for script in examples/*.py; do
        script_name=$(basename "$script" .py)
        echo "Running $script_name..."

        if uv run python "$script" >> "logs/e2e-$script_name.log" 2>&1; then
            echo "✓ $script_name completed successfully"
        else
            echo "✗ $script_name failed (check logs/e2e-$script_name.log)"
            failed=$((failed + 1))
        fi
    done

    # Capture container logs
    echo "Capturing container logs..."
    docker compose logs downloader > logs/e2e-downloader.log 2>&1
    docker compose logs redis > logs/e2e-redis.log 2>&1

    # Capture access logs if they exist in the container
    docker compose exec -T downloader cat /var/log/access.log > logs/e2e-access.log 2>/dev/null || echo "No access log found in container"

    # Clean up
    echo "Cleaning up..."
    docker compose down -v

    if [ "$failed" -ne 0 ]; then
        echo "E2E test failed: $failed example script(s) failed"
        exit 1
    fi

    echo "E2E test completed! Check logs/ directory for details."
