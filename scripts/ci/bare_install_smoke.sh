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

# As expect, and the needle must appear exactly once: a hint printed twice (a log
# line, then the handler) reads as two failures.
expect_once() { # expect_once <label> <wanted-rc> <needle> <command...>
  local label=$1 want=$2 needle=$3 out rc flat stripped count
  shift 3
  set +e
  out=$("$@" 2>&1 </dev/null)
  rc=$?
  set -e
  flat=$(printf '%s' "$out" | tr -s '[:space:]' ' ')
  stripped=${flat//"$needle"/}
  count=$(( (${#flat} - ${#stripped}) / ${#needle} ))
  if [ "$rc" -ne "$want" ] || [ "$count" -ne 1 ] || [[ "$out" == *Traceback* ]]; then
    echo "::error::bare-install smoke '$label': rc=$rc (want $want), '$needle' appeared $count times (want 1)"
    printf '%s\n' "$out"
    exit 1
  fi
  echo "ok: $label"
}

expect "mcp serve" 1 "pip install 'data-product-forge[mcp]'" "$FLUID" mcp serve
expect "scaffold-ide" 1 "pip install 'data-product-forge[mcp]'" \
  "$FLUID" scaffold-ide --target claude-code --out ide
expect "output-port serve" 1 "pip install 'data-product-forge[mcp]'" \
  "$FLUID" mcp output-port serve "$CONTRACT"
[ ! -e ide ] || { echo "::error::scaffold-ide wrote files without the mcp SDK"; exit 1; }
expect "ai forge" 1 "pip install 'data-product-forge[litellm]'" \
  env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u GEMINI_API_KEY -u GOOGLE_API_KEY \
  "$FLUID" forge --llm-provider openai --non-interactive
expect "ai forge alternatives" 1 "--llm-provider claude-code" \
  "$FLUID" forge --llm-provider openai --non-interactive
printf 'name: sales\ngrain: order\n' > intent.yaml
expect_once "data-model from-intent hint once" 1 "pip install 'data-product-forge[litellm]'" \
  env -u OPENAI_API_KEY "$FLUID" forge data-model from-intent intent.yaml \
  --output model.yaml --llm-provider openai
# In a bordered panel that wraps, so match the extra spelling alone.
expect "ai status keeps the extra" 0 "data-product-forge[litellm]" \
  env FLUID_LLM_PROVIDER=openai OPENAI_API_KEY=smoke-not-a-key "$FLUID" ai status
expect "ai setup stops before a key prompt" 1 "pip install 'data-product-forge[litellm]'" \
  "$FLUID" ai setup
# The hint sits in a bordered panel that wraps, so match the extra spelling alone.
expect "doctor readiness" 0 "data-product-forge[litellm]" \
  env GEMINI_API_KEY=smoke-not-a-key "$FLUID" doctor
expect "template forge" 0 "contract.fluid.yaml" \
  "$FLUID" forge --template starter --non-interactive --yes

# OPENLINEAGE_URL without the client: the emitter warns once with the extra and
# does not raise. `fluid apply` reaches it only through a real OpenTofu apply
# (needs the tofu binary), so drive the same emit path directly.
expect_once "lineage warns once" 0 "pip install 'data-product-forge[lineage]'" \
  env OPENLINEAGE_URL=http://127.0.0.1:9 "$VENV/bin/python" -I -c "
from fluid_build.api.lineage import RunEventType
from fluid_build.build_runners._lineage import emit_apply_event, resolve_lineage_emitter
from fluid_build.cli import _setup_enhanced_logging
_setup_enhanced_logging('WARNING', None)  # the CLI's handlers, as in a real apply
emitter = resolve_lineage_emitter()
for kind in (RunEventType.START, RunEventType.COMPLETE):
    emit_apply_event(emitter, {'id': 'p', 'exposes': []}, event_type=kind,
                     event_time='2026-11-03T00:00:00Z', run_id='01SMOKE',
                     run_started_at='2026-11-03T00:00:00Z', provider='aws')
"
echo "Bare-install smoke passed"
