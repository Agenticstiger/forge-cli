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

"""Prompt loading helpers."""

from __future__ import annotations

from importlib import resources
from typing import Any

from jinja2 import Template


def load_prompt_text(relative_path: str) -> str:
    """Load a prompt asset from the staged copilot package."""
    prompt_root = resources.files("fluid_build.copilot.prompts")
    return (prompt_root / relative_path).read_text(encoding="utf-8")


def render_prompt(relative_path: str, **context: Any) -> str:
    """Render a prompt template with Jinja2."""
    return Template(load_prompt_text(relative_path)).render(**context)
