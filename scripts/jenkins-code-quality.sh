#!/bin/bash
# Jenkins CI/CD - CLI Code Quality Script

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "   CLI: Code Quality Analysis"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Install dependencies. Hash-locked: requirements/ci-tools.in names the tools,
# ci-tools.txt carries every wheel hash, and --require-hashes refuses anything
# not in it.
python3 -m pip install --user --break-system-packages --quiet \
    --require-hashes -r "$(dirname "$0")/../requirements/ci-tools.txt"
export PATH="$HOME/.local/bin:$PATH"

# Ruff
echo "\n[1/2] Running Ruff linter..."
ruff check fluid_build/ --output-format=json > ruff-report.json || true
ruff check fluid_build/ || echo "Ruff found issues"

# Black
echo "\n[2/2] Checking Black formatting..."
black --check fluid_build/ tests/ || echo "Black formatting issues found"

echo "\n✅ Code quality analysis complete"
