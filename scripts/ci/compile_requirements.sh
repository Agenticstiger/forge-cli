#!/usr/bin/env bash
# Regenerate the hash-locked requirements files under requirements/ from their
# .in sources. Run from anywhere; needs uv (https://docs.astral.sh/uv/).
#
#   scripts/ci/compile_requirements.sh              # all lock files
#   scripts/ci/compile_requirements.sh ci-tools     # one of them
#
# --universal resolves once for every platform and Python version at or above
# --python-version, so one file serves the whole CI matrix (3.10 to 3.14) and
# the Jenkins nodes. --generate-hashes records every wheel and sdist hash, so
# `pip install --require-hashes -r <file>` rejects anything else.
#
# A requirements/<name>.overrides file, when present, is passed as --override:
# its pins replace whatever version a dependency asks for. A lock built with
# one must be installed with --no-deps, or pip re-checks the original pin.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

compile_one() {
  local name="$1" python_version="$2"
  local extra=()
  if [ -f "requirements/${name}.overrides" ]; then
    extra+=(--override "requirements/${name}.overrides")
  fi
  uv pip compile "requirements/${name}.in" \
    --universal \
    --generate-hashes \
    --python-version "${python_version}" \
    ${extra[@]+"${extra[@]}"} \
    --output-file "requirements/${name}.txt"
}

targets=("$@")
if [ "${#targets[@]}" -eq 0 ]; then
  targets=(ci-tools ci-security ci-typecheck verify-image release-tools)
fi

for target in "${targets[@]}"; do
  case "${target}" in
    ci-tools | ci-security | ci-typecheck) compile_one "${target}" 3.10 ;;
    verify-image | release-tools) compile_one "${target}" 3.12 ;;
    *)
      echo "unknown lock file: ${target}" >&2
      exit 2
      ;;
  esac
done
