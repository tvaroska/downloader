# Install dependencies
install:
    uv sync --upgrade
    pre-commit install

# Format and lint code
fmt:
    ruff check --fix

# Run tests
test:
    uv run pytest tests/ -v

# Run development server
dev:
    uv run python -m uvicorn src.downloader.main:app --host 0.0.0.0 --port 8000 --reload

# Build and push Docker image
docker:
    docker build -t ghcr.io/tvaroska/downloader .
    docker push ghcr.io/tvaroska/downloader

# Clean build artifacts
clean:
    find . -type f -name "*.pyc" -delete
    find . -type d -name "__pycache__" -delete
    rm -rf .pytest_cache htmlcov .coverage