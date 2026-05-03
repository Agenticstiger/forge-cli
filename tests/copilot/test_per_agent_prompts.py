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

"""Phase 3.9 — per-agent prompt fragments.

Before this phase, every staged agent shared one system prompt
template; the LogicalAgent's "you are a senior data modeller" voice
was identical to the BuilderAgent's "you are a transformation
engineer". Per-agent voice yamls under
``agent_specs/_defaults/agent_voice/<stage>.yaml`` give each stage
a distinct identity that ops can edit without a Python change.

Pin:

1. **Every shipped voice yaml loads cleanly** — yaml parses,
   ``system_prompt`` key is non-empty.
2. **`agent_voice(stage)` returns the loaded fragment** for known
   stages.
3. **Unknown stages return ""** — no crash, no fallback voice.
4. **Stage names are case-insensitive**.
5. **Each shipped voice mentions the stage's specific role** so a
   diff-only-trim can't accidentally make every agent say the same
   thing.
6. **Voice content is hot-reloadable** for tests via the
   ``_load_agent_voices`` re-loader (operator can patch a yaml then
   re-import; pin the loader path).
"""

from __future__ import annotations

import pytest

from fluid_build.cli.forge_copilot_prompts import (
    _AGENT_VOICES,
    _load_agent_voices,
    agent_voice,
)

_EXPECTED_STAGES = (
    "logical",
    "builder",
    "transformation",
    "readme",
    "validator",
    "critic",
    "contract_forge",
)


# ---------------------------------------------------------------------------
# Behaviour 1 — all expected voices ship and load
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", _EXPECTED_STAGES)
def test_each_expected_stage_has_a_voice(stage):
    voice = agent_voice(stage)
    assert voice, f"voice for stage {stage!r} is empty / missing"
    assert isinstance(voice, str)


@pytest.mark.parametrize("stage", _EXPECTED_STAGES)
def test_voice_is_non_trivial(stage):
    """A voice that's only whitespace + a newline isn't a voice."""
    voice = agent_voice(stage)
    stripped = voice.strip()
    assert len(stripped) > 50, f"voice for {stage!r} is too short to be useful: {stripped!r}"


# ---------------------------------------------------------------------------
# Behaviour 2 — agent_voice returns the loaded fragment
# ---------------------------------------------------------------------------


def test_agent_voice_returns_fragment_for_known_stage():
    out = agent_voice("logical")
    assert "LogicalAgent" in out


def test_agent_voice_unknown_stage_returns_empty():
    assert agent_voice("totally-not-a-stage-9999") == ""
    assert agent_voice("") == ""
    assert agent_voice(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Behaviour 3 — case-insensitive
# ---------------------------------------------------------------------------


def test_agent_voice_is_case_insensitive():
    assert agent_voice("LOGICAL") == agent_voice("logical")
    assert agent_voice("  Logical  ") == agent_voice("logical")


# ---------------------------------------------------------------------------
# Behaviour 4 — each stage has a distinct identity
# ---------------------------------------------------------------------------


def test_each_voice_mentions_its_stage_specific_role():
    """A diff-only-trim must not collapse every voice to identical
    boilerplate. Pin one per-stage role marker each."""
    role_markers = {
        "logical": ("LogicalAgent", "data modeller"),
        "builder": ("BuilderAgent", "contract"),
        "transformation": ("TransformationAgent", "SQL"),
        "readme": ("ReadmeAgent",),
        "validator": ("ValidatorAgent",),
        "critic": ("CriticAgent",),
        "contract_forge": ("ContractForgeAgent",),
    }
    for stage, markers in role_markers.items():
        voice = agent_voice(stage)
        for marker in markers:
            assert marker in voice, f"voice for stage {stage!r} missing role marker {marker!r}"


def test_voices_are_distinct():
    """Two stages must not have the same voice (regression guard
    against accidental yaml duplication)."""
    seen = set()
    for stage in _EXPECTED_STAGES:
        voice = agent_voice(stage)
        assert voice not in seen, f"voice for {stage!r} is identical to a previous stage's voice"
        seen.add(voice)


# ---------------------------------------------------------------------------
# Behaviour 5 — loader is re-callable
# ---------------------------------------------------------------------------


def test_loader_re_run_returns_same_set_of_stages():
    """Re-running the loader must surface the same stage keys; tests
    that patch a voice yaml on disk + re-import expect this property."""
    fresh = _load_agent_voices()
    assert set(fresh.keys()) == set(_AGENT_VOICES.keys())
    for k, v in fresh.items():
        assert _AGENT_VOICES[k] == v
