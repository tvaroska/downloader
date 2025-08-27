install:
	pip install uv
	uv pip install --system -e .[dev]

install-dev: install
	pre-commit install

format:
	black src tests
	isort src tests

lint:
	flake8 src tests
	mypy src

test:
	pytest tests/ -v

test-cov:
	pytest tests/ --cov=src --cov-report=html --cov-report=term

docker-build:
	docker build -t downloader:latest .

docker-run:
	docker-compose up --build

docker-test:
	docker-compose up -d
	sleep 5
	curl -f http://localhost:8000/health
	docker-compose down

dev:
	python -m uvicorn src.downloader.main:app --host 0.0.0.0 --port 8000 --reload

clean:
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	rm -rf .pytest_cache
	rm -rf htmlcov
	rm -rf .coverage

.PHONY: install install-dev format lint test test-cov docker-build docker-run docker-test dev clean