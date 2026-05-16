#!/usr/bin/env python3
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

"""GitHub Models smoke test — exercise the forge-cli LLM path with no API key.

Run inside GitHub Actions with ``permissions: models: read``: the built-in
``GITHUB_TOKEN`` (exported as ``GITHUB_API_KEY``) authenticates litellm's
``github/`` provider, so the LLM codepath gets real-inference coverage with
no provider secret at all.

Env contract (set by ``.github/workflows/llm-smoke.yml``):

* ``FLUID_LLM_PROVIDER=github``
* ``FLUID_LLM_MODEL=<an OpenAI-family GitHub Models id, e.g. gpt-4o-mini>``
* ``GITHUB_API_KEY=<the workflow's GITHUB_TOKEN>``

Resolves through ``resolve_llm_config`` so this also covers the real
provider / model / key resolution path, not just the raw completion call.
Exits 0 on a non-empty response; non-zero (with a diagnostic) otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    from fluid_build.cli.forge_copilot_llm_providers import (
        call_llm,
        get_llm_provider,
        resolve_llm_config,
    )

    env = dict(os.environ)
    if env.get("FLUID_LLM_PROVIDER", "").strip().lower() != "github":
        print(
            "llm_smoke: FLUID_LLM_PROVIDER must be 'github' for this smoke test.",
            file=sys.stderr,
        )
        return 2
    if not env.get("GITHUB_API_KEY"):
        print(
            "llm_smoke: GITHUB_API_KEY is not set (expected the workflow's "
            "GITHUB_TOKEN, granted the 'models: read' permission).",
            file=sys.stderr,
        )
        return 2

    config = resolve_llm_config(argparse.Namespace(), env)
    provider = get_llm_provider(config.provider)
    print(f"llm_smoke: provider={config.provider} model={config.model}")

    try:
        text = call_llm(
            provider,
            config,
            "You are a CI smoke test. Reply with a single word.",
            "Reply with the word: OK",
        )
    except Exception as exc:  # noqa: BLE001 — smoke test: report, don't crash
        print(
            f"llm_smoke: FAIL — GitHub Models call raised {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    if not (text or "").strip():
        print(
            "llm_smoke: FAIL — GitHub Models returned an empty response.",
            file=sys.stderr,
        )
        return 1

    print(f"llm_smoke: OK — GitHub Models responded ({text.strip()[:80]!r}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
