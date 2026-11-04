# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Backwards-compat alias — this module moved to ``fluid_build.llm.litellm_backend``.

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

from fluid_build.llm import litellm_backend as _relocated

_sys.modules[__name__] = _relocated
