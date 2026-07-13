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

"""Per-agent "voice" prompt fragments (tier-0 shared leaf).

Each staged agent (``logical`` / ``builder`` / ``transformation`` / ``readme``
/ ``validator`` / ``critic`` / ``contract_forge``) has a role-identity fragment
in ``cli/agent_specs/_defaults/agent_voice/<stage>.yaml``. Both the CLI prompt
builder (:mod:`fluid_build.cli.forge_copilot_prompts`) and the copilot agents
(:class:`fluid_build.copilot.agents.base.BaseStageAgent`) prepend these to the
system prompt.

This module is a **tier-0 shared leaf** — stdlib + ``pyyaml`` only, with no
``fluid_build.*`` upstreams. It sits below both ``cli`` and ``copilot`` so
``copilot.agents.base`` can read the voice fragment without importing anything
under ``cli`` (the ``copilot -> cli`` edge the ``[tool.importlinter]`` contracts
forbid). ``forge_copilot_prompts`` re-exports :func:`agent_voice` (and the
``_AGENT_VOICES`` / ``_load_agent_voices`` internals) so existing call sites and
test patches keep resolving via that namespace.

The voice YAML files stay in ``cli/agent_specs/_defaults/agent_voice/`` (package
data alongside the other prompt defaults); this leaf resolves that directory by
a package-relative path — a *data-file* reference, not a code import of ``cli``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

import yaml

# The voice fragments are bundled package data under
# ``cli/agent_specs/_defaults/agent_voice`` (kept next to the other prompt
# defaults consumed by ``forge_copilot_prompts``). Locating the directory by a
# package-relative path — ``fluid_build/`` is this module's parent — is a data
# lookup, not an import edge, so the leaf carries no ``cli`` code dependency.
_AGENT_VOICE_DIR: Path = (
    Path(__file__).with_name("cli") / "agent_specs" / "_defaults" / "agent_voice"
)


def _load_agent_voices() -> Mapping[str, str]:
    """Load per-agent voice fragments under ``_defaults/agent_voice/``.

    Phase 3.9 — splits the single shared system-prompt voice into one
    yaml file per agent so each stage's role identity ("you are the
    FLUID LogicalAgent — a senior data modeller …") lives next to the
    other prompt fragments. Loaded once at module import; missing /
    malformed files fall back to empty strings so the wiring is
    additive and a partial install can't crash the CLI.

    Keys are agent stage names (``logical``, ``builder``,
    ``transformation``, ``readme``, ``validator``, ``critic``,
    ``contract_forge``). Callers compose these on top of their
    existing system prompt via :func:`agent_voice` below.
    """
    voices: dict[str, str] = {}
    voice_dir = _AGENT_VOICE_DIR
    if not voice_dir.is_dir():
        return voices
    for path in sorted(voice_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(raw, Mapping):
            continue
        text = raw.get("system_prompt")
        if isinstance(text, str):
            voices[path.stem] = text.rstrip() + "\n"
    return voices


_AGENT_VOICES: Mapping[str, str] = _load_agent_voices()


def agent_voice(stage: str) -> str:
    """Return the per-agent voice fragment for ``stage`` (or "" if none).

    Phase 3.9 public surface. Agents that want their per-stage voice
    auto-prepended to the system prompt call this and concatenate
    the result. Empty string when the stage doesn't have a voice
    file — keeps callers from crashing on unrecognised stage names.
    """
    return _AGENT_VOICES.get((stage or "").strip().lower(), "")


__all__ = ["_AGENT_VOICES", "_load_agent_voices", "agent_voice"]
