#!/bin/bash
# Jenkins CI/CD - CLI Commands Test Script
# Version: 1.1.0 (Updated: 2026-01-07 - Added version tracking)

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "   🧪 CLI Command Tests"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 Script Version: 1.1.0"
echo "📂 Current Directory: $(pwd)"
echo "🔍 Git Commit: $(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Change to CLI directory (important for imports to work)
cd "$(dirname "$0")/.." || exit 1
CLI_ROOT=$(pwd)
echo "📂 CLI Root: ${CLI_ROOT}"

# Install test dependencies
python3 -m pip install --user --break-system-packages --quiet \
    -r requirements.txt pytest pytest-cov coverage pytest-html
export PATH="$HOME/.local/bin:$PATH"

# Create output directories
mkdir -p test-reports/cli coverage/cli

# Set PYTHONPATH to include tests directory for module imports
export PYTHONPATH="${CLI_ROOT}/tests:${CLI_ROOT}:${PYTHONPATH}"
echo "🔧 PYTHONPATH: ${PYTHONPATH}"

# Verify test_runner.py exists
if [ ! -f "tests/test_runner.py" ]; then
    echo "❌ ERROR: test_runner.py not found in tests directory!"
    echo "Files in tests directory:"
    ls -la tests/*.py
    exit 1
fi
echo "✅ Found test_runner.py at: $(pwd)/tests/test_runner.py"

# Run CLI tests using jenkins_runner
echo "🚀 Running CLI tests..."
python3 tests/jenkins_runner.py \
    --category cli \
    --coverage \
    --ai-logs \
    --json-report test-reports/cli/results.json \
    --junit-xml test-reports/cli/junit.xml \
    --fail-under 5.0 || exit 1

echo "\n✅ CLI command tests passed"
