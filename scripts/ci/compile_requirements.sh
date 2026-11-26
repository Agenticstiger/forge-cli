#!/usr/bin/env bash
# Regenerate the hash-locked requirements files under requirements/ from their
# .in sources. Run from anywhere; needs uv (https://docs.astral.sh/uv/).
#
#   scripts/ci/compile_requirements.sh              # all lock files
#   scripts/ci/compile_requirements.sh ci-tools     # one of them
#
# --universal resolves once for every platform and Python version at or above
# --python-version, so a lock serves every interpreter from there up. Each lock
# is compiled for the oldest Python that installs it (see the case below): the
# CI tools and the type checker run across the matrix, so they start at 3.10.
# --generate-hashes records every wheel and sdist hash, so
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
  targets=(ci-tools ci-security ci-typecheck verify-image singer-tap release-tools release-image)
fi

for target in "${targets[@]}"; do
  case "${target}" in
    ci-tools | ci-typecheck) compile_one "${target}" 3.10 ;;
    ci-security | verify-image | singer-tap | release-tools) compile_one "${target}" 3.12 ;;
    release-image) compile_one "${target}" 3.14 ;;
    *)
      echo "unknown lock file: ${target}" >&2
      exit 2
      ;;
  esac
done
