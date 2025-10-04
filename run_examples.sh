#!/bin/bash

# Script to run all example scripts in the examples/ directory using uv run

set -e  # Exit on any error

EXAMPLES_DIR="examples"
SUCCESSFUL=0
FAILED=0

echo "=========================================="
echo "Running all examples in ${EXAMPLES_DIR}/"
echo "=========================================="

# Check if examples directory exists
if [ ! -d "$EXAMPLES_DIR" ]; then
    echo "❌ Examples directory not found"
    exit 1
fi

# Find all Python files in examples directory
SCRIPTS=($(find "$EXAMPLES_DIR" -name "*.py" | sort))

if [ ${#SCRIPTS[@]} -eq 0 ]; then
    echo "❌ No Python example scripts found"
    exit 1
fi

echo "Found ${#SCRIPTS[@]} example scripts:"
for script in "${SCRIPTS[@]}"; do
    echo "  - $(basename "$script")"
done

# Run each example
for script in "${SCRIPTS[@]}"; do
    echo ""
    echo "=========================================="
    echo "Running: $script"
    echo "=========================================="

    if uv run python "$script"; then
        echo "✅ $(basename "$script") completed successfully"
        ((SUCCESSFUL++))
    else
        echo "❌ $(basename "$script") failed"
        ((FAILED++))
    fi
done

# Summary
echo ""
echo "=========================================="
echo "SUMMARY"
echo "=========================================="
echo "✅ Successful: $SUCCESSFUL"
echo "❌ Failed: $FAILED"
echo "📊 Total: ${#SCRIPTS[@]}"

if [ $FAILED -gt 0 ]; then
    echo "❌ Some examples failed"
    exit 1
else
    echo "🎉 All examples completed successfully!"
fi
