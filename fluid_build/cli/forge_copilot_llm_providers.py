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

"""Backwards-compat alias — this module moved to ``fluid_build.llm.providers``.

The FLUID LLM runtime was relocated out of ``cli`` into ``fluid_build.llm``
(and a couple of tier-0 leaves) to break the ``copilot -> cli`` import cycle;
see ``fluid_build/llm/__init__.py``. This file is a ``sys.modules`` object-alias
of the relocated module: importing this legacy path yields the *same module
object*, so every existing ``from fluid_build.cli.<old> import X`` call site and
every ``patch("fluid_build.cli.<old>.Y")`` test target keeps resolving to the
canonical implementation with zero churn.
"""

from __future__ import annotations

import sys as _sys

from fluid_build.llm import providers as _relocated

_sys.modules[__name__] = _relocated
