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

"""Coverage for ``fluid_build.copilot.store.skills`` (D3).

The ``skills/*`` namespace gives every Store backend a place to hold the
compiled-skills payload that previously only lived in the per-workspace
file ``.fluid/skills.compiled.json``. These tests pin:

* **Key stability** — ``workspace_key`` is deterministic across runs and
  case-sensitive about the path.
* **Round-trip** — write then load returns the same dict via a
  ``FileBackend`` (default) and a ``NullBackend`` (returns None).
* **Best-effort semantics** — ``mirror_skills_to_store`` and
  ``load_skills_from_store_best_effort`` never raise even when the
  backing store is broken; failures show up as DEBUG logs only.
* **File-cache integration** — ``write_compiled_skills`` side-effects
  the mirror; ``load_compiled_skills`` falls through to the store when
  neither the compiled JSON nor the raw YAML is available.
* **Defensive loads** — a non-dict value in ``skills/<key>`` (from a
  rogue shared-store entry) is ignored with a DEBUG log, not surfaced
  as skills data.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.copilot.store.backends.file import FileBackend
from fluid_build.copilot.store.backends.null import NullBackend
from fluid_build.copilot.store.base import Store, StoreRecord
from fluid_build.copilot.store.skills import (
    SKILLS_NAMESPACE,
    load_skills_from_store,
    load_skills_from_store_best_effort,
    mirror_skills_to_store,
    workspace_key,
    write_skills_to_store,
)


class TestWorkspaceKey:
    def test_deterministic_across_calls(self, tmp_path: Path):
        a = workspace_key(tmp_path)
        b = workspace_key(tmp_path)
        assert a == b
        assert len(a) == 16  # SHA1 prefix length we pinned

    def test_different_paths_produce_different_keys(self, tmp_path: Path):
        workspace_a = tmp_path / "a"
        workspace_b = tmp_path / "b"
        workspace_a.mkdir()
        workspace_b.mkdir()
        assert workspace_key(workspace_a) != workspace_key(workspace_b)

    def test_relative_path_resolves_to_absolute_before_hashing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A caller that passes ``Path(".")`` from the workspace root
        must hash the same as a caller passing the absolute path."""
        monkeypatch.chdir(tmp_path)
        relative_key = workspace_key(Path("."))
        absolute_key = workspace_key(tmp_path)
        assert relative_key == absolute_key


class TestRoundTripWithFileBackend:
    @pytest.fixture
    def store(self, tmp_path: Path) -> FileBackend:
        return FileBackend(root=tmp_path / "store")

    def test_write_then_load_returns_same_payload(self, store: FileBackend, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        compiled = {"industry": "Retail", "canonical_model": "NRF ARTS"}
        record = write_skills_to_store(store, workspace, compiled)
        assert record.namespace == SKILLS_NAMESPACE
        assert record.value == compiled
        assert record.metadata["workspace_root"] == str(workspace.resolve())

        loaded = load_skills_from_store(store, workspace)
        assert loaded == compiled

    def test_load_returns_none_when_namespace_is_empty(self, store: FileBackend, tmp_path: Path):
        assert load_skills_from_store(store, tmp_path) is None

    def test_workspace_isolation(self, store: FileBackend, tmp_path: Path):
        ws_a = tmp_path / "a"
        ws_b = tmp_path / "b"
        ws_a.mkdir()
        ws_b.mkdir()
        write_skills_to_store(store, ws_a, {"industry": "A"})
        write_skills_to_store(store, ws_b, {"industry": "B"})
        assert load_skills_from_store(store, ws_a) == {"industry": "A"}
        assert load_skills_from_store(store, ws_b) == {"industry": "B"}

    def test_ttl_is_honoured(self, store: FileBackend, tmp_path: Path):
        """A zero-TTL record is considered expired at read time and must
        return ``None`` — sanity check that FileBackend's expiry still
        fires through the skills helper."""
        import time

        workspace = tmp_path / "ws"
        workspace.mkdir()
        write_skills_to_store(store, workspace, {"industry": "R"}, ttl=1)
        # Force expiry by sleeping past TTL.
        time.sleep(1.1)
        assert load_skills_from_store(store, workspace) is None


class TestNullBackend:
    def test_null_backend_round_trip_returns_none(self, tmp_path: Path):
        store = NullBackend()
        # NullBackend accepts puts and discards them; load must be None.
        write_skills_to_store(store, tmp_path, {"industry": "R"})
        assert load_skills_from_store(store, tmp_path) is None


class TestDefensiveLoad:
    def test_non_dict_value_is_ignored(self, tmp_path: Path):
        store = FileBackend(root=tmp_path / "store")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        # Bypass write_skills_to_store and put a non-dict directly —
        # simulating a rogue shared-store entry from an older version.
        key = workspace_key(workspace)
        store.put(SKILLS_NAMESPACE, key, ["not", "a", "dict"])
        assert load_skills_from_store(store, workspace) is None


# ----------------------------------------------------------------------
# Best-effort mirror (never raises)
# ----------------------------------------------------------------------


class _ExplodingStore(Store):
    """A Store that blows up on every operation. Used to prove that
    mirror/load helpers swallow failures."""

    def get(self, ns, key):
        raise RuntimeError("store is on fire (get)")

    def put(self, ns, key, value, *, ttl=None, metadata=None, fluid_version=None):
        raise RuntimeError("store is on fire (put)")

    def query(self, ns, *, filter=None, limit=10):
        raise RuntimeError("store is on fire (query)")

    def search(self, ns, query, *, mode="exact", limit=10):
        raise RuntimeError("store is on fire (search)")

    def clear(self, ns=None):
        raise RuntimeError("store is on fire (clear)")


class TestMirrorBestEffort:
    def test_mirror_with_explicit_store(self, tmp_path: Path):
        store = FileBackend(root=tmp_path / "store")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        record = mirror_skills_to_store(workspace, {"industry": "R"}, store=store)
        assert record is not None
        assert record.value == {"industry": "R"}

    def test_mirror_swallows_failures(self, tmp_path: Path):
        """An exploding store must not propagate errors — the file
        write is the primary artefact; the mirror is additive."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        # No raise:
        result = mirror_skills_to_store(workspace, {"industry": "R"}, store=_ExplodingStore())
        assert result is None

    def test_mirror_resolves_store_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """When no store is passed, the mirror resolves one from
        FLUID_STORE_BACKEND. Point it at a tmp FileBackend via env so
        we don't touch ``~/.fluid``."""
        monkeypatch.setenv("FLUID_STORE_BACKEND", "file")
        monkeypatch.setenv("FLUID_STORE_ROOT", str(tmp_path / "store"))
        workspace = tmp_path / "ws"
        workspace.mkdir()
        record = mirror_skills_to_store(workspace, {"industry": "R"})
        assert record is not None

    def test_load_best_effort_swallows_failures(self, tmp_path: Path):
        """Same guarantee on the read side."""
        assert load_skills_from_store_best_effort(tmp_path, store=_ExplodingStore()) is None

    def test_load_best_effort_returns_value_when_store_works(self, tmp_path: Path):
        store = FileBackend(root=tmp_path / "store")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        write_skills_to_store(store, workspace, {"industry": "R"})
        assert load_skills_from_store_best_effort(workspace, store=store) == {"industry": "R"}


# ----------------------------------------------------------------------
# Integration with the existing file-based skills cache
# ----------------------------------------------------------------------


class TestFileCacheIntegration:
    """``write_compiled_skills`` now also mirrors to the store, and
    ``load_compiled_skills`` falls through to the store when the file
    is absent. These tests pin both sides."""

    def _env_isolate(self, monkeypatch: pytest.MonkeyPatch, store_root: Path):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "file")
        monkeypatch.setenv("FLUID_STORE_ROOT", str(store_root))

    def test_write_mirrors_to_store(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from fluid_build.cli.forge_copilot_skills_cache import (
            clear_compiled_skills_cache,
            write_compiled_skills,
        )
        from fluid_build.copilot.store.factory import resolve_store

        self._env_isolate(monkeypatch, tmp_path / "store")
        clear_compiled_skills_cache()

        workspace = tmp_path / "ws"
        workspace.mkdir()
        compiled = {"industry": "Retail", "canonical_model": "NRF ARTS"}
        write_compiled_skills(workspace, compiled)

        # File exists as before
        file_path = workspace / ".fluid" / "skills.compiled.json"
        assert file_path.is_file()

        # Store also has the mirror
        store = resolve_store(workspace_root=workspace)
        assert load_skills_from_store(store, workspace) == compiled

    def test_load_falls_through_to_store_when_file_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Simulate a teammate who has only the shared store (no local
        .fluid/skills.compiled.json, no .fluid/skills.yaml)."""
        from fluid_build.cli.forge_copilot_skills_cache import (
            clear_compiled_skills_cache,
            load_compiled_skills,
        )
        from fluid_build.copilot.store.factory import resolve_store

        self._env_isolate(monkeypatch, tmp_path / "store")
        clear_compiled_skills_cache()

        workspace = tmp_path / "ws"
        workspace.mkdir()
        # Populate the store directly.
        store = resolve_store(workspace_root=workspace)
        write_skills_to_store(store, workspace, {"industry": "Retail"})

        # No file anywhere; loader must still find the skills via the store.
        loaded = load_compiled_skills(workspace)
        assert loaded == {"industry": "Retail"}

    def test_load_prefers_file_over_store(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """When both exist, the file wins — the store is a mirror, not
        an override. This preserves the existing ``write → load``
        round-trip and keeps per-workspace edits authoritative."""
        from fluid_build.cli.forge_copilot_skills_cache import (
            clear_compiled_skills_cache,
            load_compiled_skills,
            write_compiled_skills,
        )
        from fluid_build.copilot.store.factory import resolve_store

        self._env_isolate(monkeypatch, tmp_path / "store")
        clear_compiled_skills_cache()

        workspace = tmp_path / "ws"
        workspace.mkdir()

        # Write the file (which also mirrors).
        write_compiled_skills(workspace, {"industry": "File Wins"})
        clear_compiled_skills_cache()

        # Now overwrite the store-only copy with something different.
        store = resolve_store(workspace_root=workspace)
        write_skills_to_store(store, workspace, {"industry": "Store Says Otherwise"})

        # load_compiled_skills must return the file contents, not the store.
        assert load_compiled_skills(workspace) == {"industry": "File Wins"}
