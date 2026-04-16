#!/bin/bash
# Jenkins CI/CD - Runtime Integration Test Script

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "   🧪 Runtime Integration Tests"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Change to CLI directory (important for imports to work)
cd "$(dirname "$0")/.." || exit 1
CLI_ROOT=$(pwd)

# Install test dependencies
python3 -m pip install --user --break-system-packages --quiet \
    pytest pytest-cov coverage
export PATH="$HOME/.local/bin:$PATH"

# Create output directories
mkdir -p test-reports/runtime coverage/runtime

# Set PYTHONPATH to include tests directory for module imports
export PYTHONPATH="${CLI_ROOT}/tests:${CLI_ROOT}:${PYTHONPATH}"

# Run runtime tests (allow failures - requires GCP credentials)
python3 tests/jenkins_runner.py \
    --category runtime \
    --coverage \
    --ai-logs \
    --json-report test-reports/runtime/results.json \
    --junit-xml test-reports/runtime/junit.xml \
    --fail-under 0.0 || true

echo "\n⚠️  Runtime tests completed (cloud credentials required for full tests)"
