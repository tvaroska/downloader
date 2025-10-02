#!/bin/bash
# Generate test coverage report

set -e

echo "========================================="
echo "Generating Test Coverage Report"
echo "========================================="
echo ""

# Run tests with coverage
pytest \
    --cov=src/downloader \
    --cov-report=term-missing \
    --cov-report=html \
    --cov-report=xml \
    -v

echo ""
echo "========================================="
echo "Coverage Report Generated"
echo "========================================="
echo ""
echo "Reports generated:"
echo "  - Terminal: (displayed above)"
echo "  - HTML: htmlcov/index.html"
echo "  - XML: coverage.xml"
echo ""
echo "To view HTML report:"
echo "  open htmlcov/index.html"
echo ""
