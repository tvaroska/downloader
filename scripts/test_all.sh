#!/bin/bash
# Run all test tiers sequentially
# Exits on first failure to save time

set -e

echo "========================================="
echo "Running Complete Test Suite"
echo "========================================="
echo ""

# Tier 1: Smoke tests
echo ">>> Tier 1: Smoke Tests"
echo "-------------------------------------------"
pytest -m smoke -v --tb=short
echo ""

# Tier 2: Unit tests
echo ">>> Tier 2: Unit Tests"
echo "-------------------------------------------"
pytest -m unit -v --tb=short
echo ""

# Tier 3: Integration tests (excluding Playwright if not available)
echo ">>> Tier 3: Integration Tests"
echo "-------------------------------------------"
if command -v playwright &> /dev/null; then
    pytest -m integration -v --tb=short
else
    echo "Warning: Playwright not found, skipping Playwright tests"
    pytest -m "integration and not requires_playwright" -v --tb=short
fi
echo ""

# API tests
echo ">>> API Tests"
echo "-------------------------------------------"
pytest tests/api/ -v --tb=short
echo ""

echo "========================================="
echo "All tests completed successfully!"
echo "========================================="
echo ""
echo "Summary:"
echo "  - Smoke tests: PASSED"
echo "  - Unit tests: PASSED"
echo "  - Integration tests: PASSED"
echo "  - API tests: PASSED"
echo ""
