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

"""Part C — live cross-agent smoke (gated, self-skipping; NOT run in CI).

These spawn the *real* agent CLI and make a billed LLM call, so they are
``@pytest.mark.integration`` and self-skip unless the binary is on PATH. Run
deliberately during the pre-PR live gate, e.g.::

    .venv/bin/python -m pytest tests/test_coding_agent_live.py -m integration -v

The truly-keyless assertion (Claude Code with no ANTHROPIC_API_KEY) is the
headline: it proves forge authored a contract using only the user's
subscription. codex/cursor/kiro need their own key and skip without it.
"""

from __future__ import annotations

import json
import os
import shutil
from types import SimpleNamespace

import pytest

from fluid_build.cli.forge_copilot_coding_agent import get_coding_agent_provider
from fluid_build.cli.forge_copilot_contract_helpers import extract_json_object
from fluid_build.cli.forge_copilot_llm_providers import resolve_llm_config

pytestmark = pytest.mark.integration

_SYSTEM = (
    "You generate FLUID DataProduct contracts. Respond with ONLY a JSON object "
    "matching the provided schema — no prose, no markdown fences."
)
_USER = "Create a minimal DataProduct contract for a daily sales table named 'sales'."


def _resolve(provider_name):
    return resolve_llm_config(
        SimpleNamespace(llm_provider=provider_name, llm_model=None),
        environ=dict(os.environ),
    )


def test_claude_code_live_keyless_envelope_roundtrip():
    if not shutil.which("claude"):
        pytest.skip("claude CLI not installed")
    provider = get_coding_agent_provider("claude-code")
    out = provider.invoke_blocking(_resolve("claude-code"), _SYSTEM, _USER)
    payload = extract_json_object(out)  # raises if not parseable
    assert isinstance(payload, dict)
    assert "contract" in payload


@pytest.mark.parametrize(
    "provider_name, binary, key_env",
    [
        ("codex", "codex", "CODEX_API_KEY"),
        ("cursor", "cursor-agent", "CURSOR_API_KEY"),
        ("kiro", "kiro-cli", "KIRO_API_KEY"),
    ],
)
def test_keyed_agent_live_envelope_roundtrip(provider_name, binary, key_env):
    if not shutil.which(binary):
        pytest.skip(f"{binary} CLI not installed")
    if not os.environ.get(key_env):
        pytest.skip(f"{key_env} not set")
    provider = get_coding_agent_provider(provider_name)
    out = provider.invoke_blocking(_resolve(provider_name), _SYSTEM, _USER)
    payload = extract_json_object(out)
    assert isinstance(payload, dict)
