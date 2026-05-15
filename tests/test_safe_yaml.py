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

"""Tests for ``fluid_build.util.safe_yaml`` — billion-laughs / anchor-bomb
denial-of-service defense for untrusted YAML (G1)."""

from __future__ import annotations

import io

import pytest

from fluid_build.util.safe_yaml import MAX_YAML_ALIASES, UnsafeYamlError, load_yaml_safe


def test_loads_plain_yaml() -> None:
    assert load_yaml_safe("a: 1\nb: [x, y]") == {"a": 1, "b": ["x", "y"]}


def test_none_returns_none() -> None:
    assert load_yaml_safe(None) is None


def test_accepts_bytes_and_streams() -> None:
    assert load_yaml_safe(b"k: v") == {"k": "v"}
    assert load_yaml_safe(io.StringIO("k: v")) == {"k": "v"}


def test_a_few_aliases_are_allowed() -> None:
    """Legitimate, modest anchor reuse must still parse."""
    doc = "anchor: &a value\nref1: *a\nref2: *a"
    assert load_yaml_safe(doc) == {"anchor": "value", "ref1": "value", "ref2": "value"}


def test_rejects_oversized_input() -> None:
    huge = "x: " + ("a" * (6 * 1024 * 1024))
    with pytest.raises(UnsafeYamlError):
        load_yaml_safe(huge)


def test_rejects_alias_bomb() -> None:
    """A classic billion-laughs payload — nested anchors each referenced
    many times — is rejected before the expensive compose step."""
    lines = ["l0: &l0 [a, a, a, a, a, a, a, a, a]"]
    prev = "l0"
    for i in range(1, 9):
        cur = f"l{i}"
        refs = ", ".join([f"*{prev}"] * 9)
        lines.append(f"{cur}: &{cur} [{refs}]")
        prev = cur
    with pytest.raises(UnsafeYamlError):
        load_yaml_safe("\n".join(lines))


def test_alias_cap_boundary() -> None:
    """One alias reference over the cap is enough to reject the document."""
    refs = "\n".join(f"r{i}: *a" for i in range(MAX_YAML_ALIASES + 1))
    with pytest.raises(UnsafeYamlError):
        load_yaml_safe("a: &a 1\n" + refs)
