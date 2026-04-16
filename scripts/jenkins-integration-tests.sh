#!/bin/bash
# Jenkins CI/CD - CLI Integration Tests Script

echo "Running CLI integration tests..."

# Set API URL to test environment
export FLUID_API_URL="http://localhost:8000"

# Run integration tests
pytest tests/ \
    -v \
    -m "integration" \
    --html=test-reports/integration-tests.html \
    --self-contained-html || exit 1
