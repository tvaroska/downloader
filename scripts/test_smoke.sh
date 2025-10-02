#!/bin/bash
# Run smoke tests only - fastest tier for quick validation

set -e

echo "========================================="
echo "Running Smoke Tests (Tier 1)"
echo "========================================="
echo ""

# Run smoke tests with minimal output
pytest -m smoke -v --tb=short

echo ""
echo "========================================="
echo "Smoke tests completed successfully!"
echo "========================================="
