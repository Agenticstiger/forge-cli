#!/usr/bin/env bash
# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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

# Ceiling for `import fluid_build.cli; build_parser(argv)` (the lazy tree `fluid
# --help` builds) on a bare install.
# Measured 299 on Python 3.12 (2026-11-04, lazy core commands; was 852);
# ceiling = measured + 5%, rounded up. Lower it when a slimming change drops
# the count.
BARE_MAX_MODULES=314

count=$(cd /tmp && "$VENV/bin/python" -I -c "
import sys; sys.argv = ['fluid', '--help']
from fluid_build.cli import build_parser; build_parser(sys.argv[1:])
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

# litellm, mcp and openlineage are extras since 0.28.0. On a bare install each
# feature that needs one must fail (or warn) with the exact install line, exit
# the documented way and print no traceback. $out is the combined output.
expect() { # expect <label> <wanted-rc> <needle> <command...>
  local label=$1 want=$2 needle=$3 out rc
  shift 3
  set +e
  out=$("$@" 2>&1 </dev/null)
  rc=$?
  set -e
  # Rich wraps long lines; compare on whitespace-collapsed text.
  local flat
  flat=$(printf '%s' "$out" | tr -s '[:space:]' ' ')
  if [ "$rc" -ne "$want" ] || [[ "$flat" != *"$needle"* ]] || [[ "$out" == *Traceback* ]]; then
    echo "::error::bare-install smoke '$label': rc=$rc (want $want), needle '$needle'"
    printf '%s\n' "$out"
    exit 1
  fi
  echo "ok: $label"
}

expect "mcp serve" 1 "pip install 'data-product-forge[mcp]'" "$FLUID" mcp serve
expect "scaffold-ide" 1 "pip install 'data-product-forge[mcp]'" \
  "$FLUID" scaffold-ide --target claude-code --out ide
[ ! -e ide ] || { echo "::error::scaffold-ide wrote files without the mcp SDK"; exit 1; }
expect "ai forge" 1 "pip install 'data-product-forge[litellm]'" \
  env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u GEMINI_API_KEY -u GOOGLE_API_KEY \
  "$FLUID" forge --llm-provider openai --non-interactive
expect "ai forge alternatives" 1 "--llm-provider claude-code" \
  "$FLUID" forge --llm-provider openai --non-interactive
# The hint sits in a bordered panel that wraps, so match the extra spelling alone.
expect "doctor readiness" 0 "data-product-forge[litellm]" \
  env GEMINI_API_KEY=smoke-not-a-key "$FLUID" doctor
expect "template forge" 0 "contract.fluid.yaml" \
  "$FLUID" forge --template starter --non-interactive --yes

# OPENLINEAGE_URL without the client: the emitter warns once with the extra and
# does not raise. `fluid apply` reaches it only through a real OpenTofu apply
# (needs the tofu binary), so drive the same emit path directly.
expect "lineage warns" 0 "pip install 'data-product-forge[lineage]'" \
  env OPENLINEAGE_URL=http://127.0.0.1:9 "$VENV/bin/python" -I -c "
from fluid_build.api.lineage import RunEventType
from fluid_build.build_runners._lineage import emit_apply_event, resolve_lineage_emitter
emitter = resolve_lineage_emitter()
for kind in (RunEventType.START, RunEventType.COMPLETE):
    emit_apply_event(emitter, {'id': 'p', 'exposes': []}, event_type=kind,
                     event_time='2026-11-03T00:00:00Z', run_id='01SMOKE',
                     run_started_at='2026-11-03T00:00:00Z', provider='aws')
"
echo "Bare-install smoke passed"
