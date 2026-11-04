# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Prompt loading helpers."""

from __future__ import annotations

from importlib import resources


def load_prompt_text(relative_path: str) -> str:
    """Load a prompt asset from the staged copilot package."""
    prompt_root = resources.files("fluid_build.copilot.prompts")
    return (prompt_root / relative_path).read_text(encoding="utf-8")
