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

"""Pins for the thread-safe, resettable lazy ``CopilotAgent`` class cache.

``forge.CopilotAgent`` is built lazily (PEP 562 ``__getattr__``) to keep the
heavy ``forge_copilot_agent`` import off the ``fluid --help`` path. The builder
is memoised with ``functools.lru_cache(maxsize=1)`` so the class object has
stable identity (``isinstance`` checks), the cache is internally lock-guarded
(no double-build race on first access from two threads), and it exposes a clean
``cache_clear()`` reset hook — replacing a hand-rolled module-global + bare
``if`` check (inspection follow-up to #315).
"""

from __future__ import annotations

import functools

from fluid_build.cli import forge


def test_copilot_agent_class_identity_is_stable():
    # Resolving forge.CopilotAgent twice (each through __getattr__ -> builder)
    # must return the *same* class object, so isinstance / identity is stable.
    first = forge.CopilotAgent
    second = forge.CopilotAgent
    assert first is second
    assert forge._build_copilot_agent_class() is first


def test_builder_is_memoised():
    assert forge._build_copilot_agent_class() is forge._build_copilot_agent_class()


def test_builder_is_lru_cache_wrapped():
    # The lock-guarded stdlib primitive is what gives us thread-safety for free;
    # assert we actually went through lru_cache rather than a bespoke global.
    assert hasattr(forge._build_copilot_agent_class, "cache_clear")
    assert hasattr(forge._build_copilot_agent_class, "cache_info")
    info = forge._build_copilot_agent_class.cache_info()
    assert isinstance(info, functools._CacheInfo)
    assert info.maxsize == 1


def test_cache_clear_rebuilds_then_re_memoises():
    first = forge._build_copilot_agent_class()
    forge._build_copilot_agent_class.cache_clear()
    second = forge._build_copilot_agent_class()
    # A fresh build after clear is a distinct class object (the body re-executes
    # the `class CopilotAgent` statement)...
    assert first is not second
    # ...and is re-memoised from there.
    assert second is forge._build_copilot_agent_class()


def test_subclasses_copilot_agent_base():
    from fluid_build.cli.forge_copilot_agent import CopilotAgentBase

    assert issubclass(forge.CopilotAgent, CopilotAgentBase)
