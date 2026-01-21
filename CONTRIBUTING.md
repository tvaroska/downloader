# Contributing to REST API Downloader

Thank you for your interest in contributing! This document provides guidelines and instructions for contributing to the project.

## Table of Contents

- [Development Setup](#development-setup)
- [Code Style](#code-style)
- [Testing](#testing)
- [Pull Request Process](#pull-request-process)
- [Project Structure](#project-structure)

## Development Setup

### Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- Redis (optional, required for batch job persistence)
- Docker (optional, for containerized testing)

### Quick Start

```bash
# Clone the repository
git clone <repository-url>
cd downloader

# Install dependencies with uv (recommended)
uv sync

# Or with pip
pip install -e ".[dev]"

# Install Playwright browsers (required for PDF/rendering features)
uv run playwright install chromium

# Set up pre-commit hooks
uv run pre-commit install

# Run the development server
uv run python run.py
```

### Environment Configuration

Copy `.env.example` to `.env` for local development:

```bash
cp .env.example .env
```

See the [Configuration Guide](docs/guides/configuration.md) for all available settings.

## Code Style

This project uses [Ruff](https://github.com/astral-sh/ruff) for linting and formatting.

### Automatic Formatting

```bash
# Format code
uv run ruff format src/ tests/ examples/

# Check and fix linting issues
uv run ruff check src/ tests/ examples/ --fix
```

### Style Guidelines

- **Line length**: 100 characters
- **Python version**: 3.10+ features are allowed
- **Type hints**: Required for all public functions
- **Docstrings**: Required for public modules, classes, and functions

### Pre-commit Hooks

Pre-commit hooks run automatically on commit:

- Trailing whitespace removal
- End-of-file fixing
- YAML validation
- Large file checks
- Ruff linting and formatting
- **Secrets detection** (detect-secrets)

To run manually:

```bash
uv run pre-commit run --all-files
```

### Secrets Detection

This project uses [detect-secrets](https://github.com/Yelp/detect-secrets) to prevent accidental commits of API keys, passwords, and other secrets.

#### If Pre-commit Fails

If the secrets hook flags something:

1. **True Positive**: Remove the secret from your code. Use environment variables instead.

2. **False Positive**: Update the baseline:
   ```bash
   # Re-scan and update baseline
   uv run detect-secrets scan . > .secrets.baseline

   # Audit to mark false positives (optional)
   uv run detect-secrets audit .secrets.baseline
   ```

#### Supported Secret Types

The scanner detects:
- AWS keys and credentials
- Private keys (RSA, SSH, etc.)
- API tokens (GitHub, Slack, Discord, etc.)
- High-entropy strings that may be passwords
- Database connection strings with credentials

## Testing

### Test Strategy

The project uses a 3-tier test strategy:

| Tier | Marker | Purpose | Typical Duration |
|------|--------|---------|------------------|
| Smoke | `@pytest.mark.smoke` | Quick validation | <3s |
| Integration | `@pytest.mark.integration` | Component testing | ~15s |
| E2E | `@pytest.mark.e2e` | Full system tests | ~60s |

### Running Tests

```bash
# Run smoke tests (fastest feedback)
uv run pytest -m smoke -v

# Run integration tests
uv run pytest -m integration -v

# Run all tests except E2E
uv run pytest tests/ --ignore=tests/e2e -v

# Run with coverage
uv run pytest tests/ --ignore=tests/e2e --cov=src/downloader --cov-report=html

# Run specific test file
uv run pytest tests/api/test_download.py -v
```

### Coverage Requirements

- **Minimum coverage**: 70% (enforced in CI)
- **Target for new code**: 80%+

## Pull Request Process

### Before Submitting

1. **Create a branch** from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** following the code style guidelines

3. **Add tests** for new functionality

4. **Run the full test suite**:
   ```bash
   uv run ruff check src/ tests/ examples/
   uv run ruff format --check src/ tests/ examples/
   uv run pytest tests/ --ignore=tests/e2e -v
   ```

5. **Update documentation** if needed

### PR Requirements

- [ ] All tests pass
- [ ] Code is formatted with Ruff
- [ ] No linting errors
- [ ] Coverage does not decrease
- [ ] Documentation updated (if applicable)
- [ ] CHANGELOG.md updated for user-facing changes

### Commit Message Format

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `test`: Adding or updating tests
- `refactor`: Code refactoring
- `chore`: Maintenance tasks
- `ci`: CI/CD changes
- `perf`: Performance improvements

**Examples:**
```
feat(api): add ?render=true query parameter for Playwright rendering
fix(security): block file:// URLs in browser rendering
docs(api): update content negotiation examples
test(browser): add integration tests for rendering
```

### Version Updates

When releasing a new version:

1. Update version in `pyproject.toml` only (single source of truth)
2. Update `CHANGELOG.md` with changes under the new version
3. Create a git tag: `git tag v0.x.0`

## Project Structure

```
downloader/
├── src/downloader/       # Main application code
│   ├── routes/           # API endpoint handlers
│   ├── browser/          # Browser rendering (Playwright)
│   ├── config.py         # Configuration management
│   ├── metrics.py        # Metrics collection
│   ├── middleware.py     # Request middleware
│   └── main.py           # FastAPI application
├── tests/                # Test suite
│   ├── api/              # API endpoint tests
│   ├── unit/             # Unit tests
│   ├── integration/      # Integration tests
│   ├── smoke/            # Smoke tests
│   └── e2e/              # End-to-end tests
├── docs/                 # Documentation
│   ├── api/              # API reference
│   ├── guides/           # How-to guides
│   └── features/         # Feature documentation
├── examples/             # Usage examples
└── monitoring/           # Prometheus/Grafana configs
```

## Getting Help

- **Documentation**: Check the `docs/` directory
- **Issues**: Search existing issues or create a new one
- **Examples**: See the `examples/` directory for usage patterns

## License

By contributing, you agree that your contributions will be licensed under the same license as the project.
