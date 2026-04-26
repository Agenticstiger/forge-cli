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

"""Defensive JSON parsing helpers."""

from __future__ import annotations

import json
from typing import Any, Optional


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _extract_balanced_json(text: str) -> Optional[str]:
    start = None
    depth = 0
    quote = None
    escape = False
    for index, char in enumerate(text):
        if start is None and char in "{[":
            start = index
            depth = 1
            continue
        if start is None:
            continue
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def safe_json_parse(text: str) -> Any:
    """Best-effort parse for partially wrapped JSON responses."""
    normalized = _strip_code_fences(text or "")
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        candidate = _extract_balanced_json(normalized)
        if not candidate:
            raise
        return json.loads(candidate)
