#!/usr/bin/env bash
# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

# Smoke a BARE wheel install (no extras): the cold import cost of
# build_parser(), then the offline pipeline stages on an example contract.
# Called from ci.yml (build-check). Not from release.yml: its build job holds
# id-token and attestations write, and a tag's commit has already passed this.
#
#   scripts/ci/bare_install_smoke.sh <venv-dir> <contract.fluid.yaml>
#
# Run from a neutral directory (the script makes its own), so nothing resolves
# from the checkout. The contract is an AWS example: `generate iac` renders it
# offline, and nothing here needs a credential.
set -euo pipefail

# Absolute, because the module-count probe below runs from /tmp.
VENV="$(cd "$1" && pwd)"
CONTRACT="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"

# Ceiling for `import fluid_build.cli; build_parser()` on a bare install.
# Measured 852 on Python 3.12 (2026-11-03); ceiling = measured + 2%, rounded
# up. Lower it when a slimming change drops the count.
BARE_MAX_MODULES=869

count=$(cd /tmp && "$VENV/bin/python" -I -c "
import sys; sys.argv = ['fluid', '--help']
from fluid_build.cli import build_parser; build_parser()
print(len(sys.modules))")
echo "Bare install: build_parser() imports ${count} modules (ceiling ${BARE_MAX_MODULES})"
if [ "$count" -ge "$BARE_MAX_MODULES" ]; then
  echo "::error::bare install imports ${count} modules on the build_parser() path (ceiling ${BARE_MAX_MODULES})"
  exit 1
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"
FLUID="$VENV/bin/fluid"
"$FLUID" validate "$CONTRACT"
"$FLUID" plan "$CONTRACT" --out plan.json
"$FLUID" bundle "$CONTRACT" --format tgz --out bundle.tgz
"$FLUID" generate iac "$CONTRACT" --out iac
"$FLUID" doctor
echo "Bare-install smoke passed"
