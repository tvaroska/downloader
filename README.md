# REST API Downloader

A high-performance web service for programmatic URL content downloading.

## Features

- Fast HTTP downloads with async support
- URL validation and sanitization
- Health check endpoint
- Docker support
- Comprehensive testing

## Quick Start

### Using Docker

```bash
docker build -t downloader .
docker run -p 8000:8000 downloader
```

### Using Docker Compose

```bash
docker-compose up
```

### Local Development

```bash
make install-dev
make dev
```

## API Endpoints

- `GET /health` - Health check
- `GET /download?url=<url>` - Download URL content (JSON response)
- `GET /download/raw?url=<url>` - Download URL content (raw response)

## Testing

```bash
make test
```

## Phase 1 Implementation

Phase 1 includes:
-  FastAPI application bootstrap
-  Basic URL validation and sanitization
-  Simple HTTP client with httpx
-  Single URL download endpoint
-  Basic error handling and HTTP status mapping
-  Health check endpoint
-  Unit tests for core functionality
-  Basic logging configuration
-  Docker development environment
-  Basic CI/CD pipeline