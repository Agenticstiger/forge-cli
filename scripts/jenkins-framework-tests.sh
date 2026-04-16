#!/bin/bash
# Jenkins CI/CD - Policy & Framework Test Script

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "   🧪 Policy & Framework Tests"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Change to CLI directory (important for imports to work)
cd "$(dirname "$0")/.." || exit 1
CLI_ROOT=$(pwd)

# Install test dependencies
python3 -m pip install --user --break-system-packages --quiet \
    pytest pytest-cov coverage pytest-html
export PATH="$HOME/.local/bin:$PATH"

# Create output directories
mkdir -p test-reports/framework coverage/framework

# Set PYTHONPATH to include tests directory for module imports
export PYTHONPATH="${CLI_ROOT}/tests:${CLI_ROOT}:${PYTHONPATH}"

# Skip - no framework tests exist yet
# Future: Add tests for policy engine, schema validation, etc.
echo "⏭️  No framework tests defined - skipping"
exit 0
