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

from pathlib import Path

from fluid_build.copilot.store.backends.file import FileBackend
from fluid_build.copilot.store.keys import generate_cache_key, workspace_fingerprint


def test_generate_cache_key_is_stable():
    key1 = generate_cache_key("claude-sonnet", "prompt", {"a": 1, "b": ["x", "y"]})
    key2 = generate_cache_key("claude-sonnet", "prompt", {"b": ["x", "y"], "a": 1})
    assert key1 == key2


# ---------------------------------------------------------------------
# Capability-matrix segment (Gap 7.3) — flipping a capability flag
# must invalidate the cache cleanly. Three cases pinned:
#
# 1. Same model + prompt + params, different capability_matrix →
#    different keys. (The whole reason for the segment.)
# 2. ``capability_matrix=None`` and ``capability_matrix={}`` are
#    equivalent so callers that don't care don't accidentally split
#    the namespace.
# 3. Capability matrix key order doesn't change the hash — the
#    canonicaliser sorts keys.
# ---------------------------------------------------------------------


def test_cache_key_changes_when_capability_flipped():
    """The whole point of the segment: a different capability matrix
    yields a different cache key, so flipping a flag invalidates the
    cache cleanly."""
    base = generate_cache_key(
        "claude-sonnet",
        "prompt",
        {},
        capability_matrix={"extended_thinking": False},
    )
    flipped = generate_cache_key(
        "claude-sonnet",
        "prompt",
        {},
        capability_matrix={"extended_thinking": True},
    )
    assert base != flipped


def test_cache_key_none_and_empty_capability_matrix_collide():
    """``None`` and ``{}`` must hash to the same key so callers that
    aren't capability-aware (older code paths, simple stages) share
    the same cache namespace as 'no capabilities active'."""
    none_key = generate_cache_key("claude-sonnet", "prompt", {}, capability_matrix=None)
    empty_key = generate_cache_key("claude-sonnet", "prompt", {}, capability_matrix={})
    assert none_key == empty_key


def test_cache_key_capability_matrix_key_order_irrelevant():
    """Two equivalent capability dicts in different key order must
    produce the same key — same property as ``params``."""
    key_a = generate_cache_key(
        "claude-sonnet",
        "prompt",
        {},
        capability_matrix={"extended_thinking": True, "cache_control": "ephemeral"},
    )
    key_b = generate_cache_key(
        "claude-sonnet",
        "prompt",
        {},
        capability_matrix={"cache_control": "ephemeral", "extended_thinking": True},
    )
    assert key_a == key_b


def test_cache_key_capability_matrix_value_change_invalidates():
    """Tweaking a capability VALUE (not just the keys) flips the key.
    Defends against a partial-update bug where the matrix is
    rebuilt with the same keys but new values."""
    a = generate_cache_key(
        "claude-sonnet",
        "prompt",
        {},
        capability_matrix={"thinking_budget": 4096},
    )
    b = generate_cache_key(
        "claude-sonnet",
        "prompt",
        {},
        capability_matrix={"thinking_budget": 8192},
    )
    assert a != b


def test_cache_key_capability_matrix_independent_of_params():
    """A param named ``capability_matrix`` is NOT the same as the
    capability_matrix kwarg. The two segments must hash separately so
    a stage that happens to use that param key doesn't pollute the
    capability segment (or vice versa)."""
    via_param = generate_cache_key(
        "claude-sonnet",
        "prompt",
        {"capability_matrix": {"extended_thinking": True}},
        capability_matrix=None,
    )
    via_kwarg = generate_cache_key(
        "claude-sonnet",
        "prompt",
        {},
        capability_matrix={"extended_thinking": True},
    )
    # If the two were conflated these would be equal — they must
    # NOT be, because their semantics differ (param vs. capability).
    assert via_param != via_kwarg


def test_workspace_fingerprint_is_stable(tmp_path: Path):
    path = tmp_path / "workspace"
    path.mkdir()
    assert workspace_fingerprint(path) == workspace_fingerprint(path)


def test_file_backend_round_trip(tmp_path: Path):
    backend = FileBackend(root=tmp_path / "store", workspace_root=tmp_path)
    backend.put("llm/modeler", "abc", {"value": 1}, metadata={"stage": "modeler"})
    record = backend.get("llm/modeler", "abc")
    assert record is not None
    assert record.value == {"value": 1}
    assert record.metadata["stage"] == "modeler"


def test_file_backend_reads_legacy_project_memory(tmp_path: Path):
    project_root = tmp_path / "project"
    legacy_dir = project_root / ".fluid"
    legacy_dir.mkdir(parents=True)
    legacy_path = legacy_dir / "copilot-memory.json"
    legacy_path.write_text('{"technique":"data_vault_2"}', encoding="utf-8")

    backend = FileBackend(root=tmp_path / "store", workspace_root=project_root)
    record = backend.get("memory/project", "workspace")
    assert record is not None
    assert record.value["technique"] == "data_vault_2"
