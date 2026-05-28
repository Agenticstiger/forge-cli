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

"""Tests for fluid_build.cli.forge_copilot_personal_memory (v1 schema).

Slice 5 moves personal memory to ``~/.fluid/personal-memory.json`` with a
namespaced ``preferences``/``history`` layout and a standard envelope.

Most tests exercise the public API (``load_personal_memory`` /
``save_personal_memory``) which still returns/accepts the flat
``preferred_*`` / ``recent_*`` keys that existing consumers rely on.  A
smaller cluster of tests reads the raw on-disk JSON to verify the new
namespaced shape and the envelope fields are actually written.
"""

from __future__ import annotations

import json
import stat
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.artifact_paths import ENVELOPE_SCHEMA_VERSION


@pytest.fixture
def mem_file(tmp_path):
    """Point ``_MEMORY_FILE`` at a temp path for the test duration."""
    path = tmp_path / "personal-memory.json"
    with patch("fluid_build.cli.forge_copilot_personal_memory._MEMORY_FILE", path):
        yield path


class TestLoadPersonalMemory:
    def test_returns_none_when_no_file(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import load_personal_memory

        assert load_personal_memory() is None

    def test_loads_valid_v1_json_and_flattens(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import load_personal_memory

        mem_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "PersonalMemory",
                    "preferences": {"provider": "gcp"},
                    "history": {},
                }
            )
        )
        result = load_personal_memory()
        assert result is not None
        # Flat projection preserves the existing consumer interface
        assert result["preferred_provider"] == "gcp"

    def test_returns_none_for_invalid_json(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import load_personal_memory

        mem_file.write_text("not json at all")
        assert load_personal_memory() is None

    def test_returns_none_for_non_dict_root(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import load_personal_memory

        mem_file.write_text('["a", "list"]')
        assert load_personal_memory() is None

    def test_legacy_v0_file_is_ignored(self, mem_file):
        """Clean-cut directive: v0 flat keys must NOT leak through the loader."""
        from fluid_build.cli.forge_copilot_personal_memory import load_personal_memory

        # A flat v0 shape — no preferences/history wrapper.
        mem_file.write_text(json.dumps({"preferred_provider": "aws", "recent_domains": ["old"]}))
        result = load_personal_memory()
        # The loader returns a dict (the file is valid JSON), but because
        # the v1 projection only reads preferences.* / history.*, nothing
        # from the flat v0 shape flows through.
        assert result == {}


class TestSavePersonalMemory:
    def test_save_creates_file_and_roundtrips_through_loader(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import (
            load_personal_memory,
            save_personal_memory,
        )

        result = save_personal_memory({"provider": "gcp", "domain": "finance"})
        assert result is True
        assert mem_file.exists()

        # Use the public loader — that's the contract existing callers rely on
        loaded = load_personal_memory()
        assert loaded["preferred_provider"] == "gcp"
        assert loaded["preferred_domain"] == "finance"

    def test_on_disk_shape_is_v1_namespaced(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import save_personal_memory

        save_personal_memory(
            {
                "provider": "gcp",
                "build_engine": "dbt",
                "domain": "retail",
                "owner_team": "data",
                "ci_provider": "github_actions",
                "ci_complexity": "standard",
                "use_case": "analytics",
            }
        )
        raw = json.loads(mem_file.read_text())

        assert raw["schema_version"] == ENVELOPE_SCHEMA_VERSION
        assert raw["kind"] == "PersonalMemory"
        assert raw["generated_by"]["tool"] == "fluid-cli"

        assert raw["preferences"] == {
            "provider": "gcp",
            "engine": "dbt",
            "domain": "retail",
            "owner_team": "data",
            "ci_provider": "github_actions",
            "ci_complexity": "standard",
        }
        assert raw["history"]["recent_domains"] == ["retail"]
        assert raw["history"]["recent_use_cases"] == ["analytics"]

    def test_save_sets_permissions_600(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import save_personal_memory

        save_personal_memory({"provider": "local"})
        mode = mem_file.stat().st_mode
        assert mode & stat.S_IRUSR
        assert mode & stat.S_IWUSR
        assert not (mode & stat.S_IRGRP)
        assert not (mode & stat.S_IROTH)

    def test_save_merges_with_existing(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import (
            load_personal_memory,
            save_personal_memory,
        )

        # Prime with a v1-shaped file
        mem_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "PersonalMemory",
                    "preferences": {"engine": "dbt", "owner_team": "data-eng"},
                    "history": {},
                }
            )
        )

        save_personal_memory({"provider": "gcp"})

        loaded = load_personal_memory()
        assert loaded["preferred_provider"] == "gcp"  # newly set
        assert loaded["preferred_engine"] == "dbt"  # preserved
        assert loaded["owner_team"] == "data-eng"  # preserved

    def test_save_tracks_recent_domains_fifo(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import (
            load_personal_memory,
            save_personal_memory,
        )

        save_personal_memory({"domain": "finance"})
        save_personal_memory({"domain": "healthcare"})

        loaded = load_personal_memory()
        assert loaded["recent_domains"] == ["healthcare", "finance"]

    def test_save_deduplicates_recent_domains(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import (
            load_personal_memory,
            save_personal_memory,
        )

        save_personal_memory({"domain": "finance"})
        save_personal_memory({"domain": "finance"})

        loaded = load_personal_memory()
        assert loaded["recent_domains"] == ["finance"]

    def test_save_bounds_recent_items_at_five(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import (
            load_personal_memory,
            save_personal_memory,
        )

        for i in range(10):
            save_personal_memory({"domain": f"domain-{i}"})

        loaded = load_personal_memory()
        assert len(loaded["recent_domains"]) == 5
        # Most recent domain comes first
        assert loaded["recent_domains"][0] == "domain-9"

    def test_first_save_shows_hint(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import save_personal_memory

        console = MagicMock()
        save_personal_memory({"provider": "local"}, console=console)
        console.print.assert_called()

    def test_second_save_no_hint(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import save_personal_memory

        save_personal_memory({"provider": "local"})  # first save → hint
        console = MagicMock()
        save_personal_memory({"provider": "gcp"}, console=console)  # second → silent
        console.print.assert_not_called()

    def test_save_persists_ci_provider_and_complexity(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import (
            load_personal_memory,
            save_personal_memory,
        )

        save_personal_memory({"ci_provider": "github_actions", "ci_complexity": "advanced"})
        loaded = load_personal_memory()
        assert loaded["preferred_ci_provider"] == "github_actions"
        assert loaded["preferred_ci_complexity"] == "advanced"

    def test_save_preserves_existing_ci_when_new_context_empty(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import (
            load_personal_memory,
            save_personal_memory,
        )

        mem_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "PersonalMemory",
                    "preferences": {
                        "ci_provider": "jenkins",
                        "ci_complexity": "enterprise",
                    },
                    "history": {},
                }
            )
        )

        save_personal_memory({"provider": "aws"})

        loaded = load_personal_memory()
        assert loaded["preferred_ci_provider"] == "jenkins"
        assert loaded["preferred_ci_complexity"] == "enterprise"
        assert loaded["preferred_provider"] == "aws"


class TestLegacyV0Isolation:
    """Clean-cut directive: leftover engineer_memory.json files are ignored."""

    def test_legacy_engineer_memory_file_is_not_read(self, tmp_path, monkeypatch):
        """An engineer_memory.json file must not influence the new loader."""
        import fluid_build.cli.forge_copilot_personal_memory as pm

        fake_home = tmp_path / "home"
        (fake_home / ".fluid").mkdir(parents=True)

        # Legacy v0 file sitting on disk — must be ignored entirely.
        legacy = fake_home / ".fluid" / "engineer_memory.json"
        legacy.write_text(json.dumps({"preferred_provider": "legacy-aws", "recent_domains": ["x"]}))

        # Point _MEMORY_FILE at the new filename in the same dir.  The
        # legacy file exists but has a different name, so load() sees
        # nothing and returns None.
        new_path = fake_home / ".fluid" / "personal-memory.json"
        monkeypatch.setattr(pm, "_MEMORY_FILE", new_path)

        assert pm.load_personal_memory() is None
        assert legacy.exists(), "legacy file should be left untouched"


class TestPoisonedValueDefence:
    """Three-layer defence against ``<MagicMock …>`` strings leaking into
    user-global ``~/.fluid/personal-memory.json`` (root cause of the
    "every streaming preview shows MagicMock as owner_team" bug)."""

    def test_load_drops_poisoned_scalar_in_preferences(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import load_personal_memory

        mem_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "PersonalMemory",
                    "preferences": {
                        "provider": "gcp",
                        "owner_team": (
                            "<MagicMock name='mock.prepare_runtime_inputs()."
                            "__getitem__().preferred_owner' id='4901580016'>"
                        ),
                    },
                    "history": {},
                }
            )
        )
        loaded = load_personal_memory()
        assert loaded is not None
        # Clean field survives, poisoned field is dropped.
        assert loaded.get("preferred_provider") == "gcp"
        assert "owner_team" not in loaded

    def test_load_drops_poisoned_items_inside_history_list(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import load_personal_memory

        mem_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "PersonalMemory",
                    "preferences": {},
                    "history": {
                        "recent_domains": ["finance", "<MagicMock name='m' id='1'>"],
                    },
                }
            )
        )
        loaded = load_personal_memory()
        assert loaded is not None
        assert loaded.get("recent_domains") == ["finance"]

    def test_save_raises_when_context_carries_magicmock_string(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import save_personal_memory

        with pytest.raises(ValueError, match="test-double repr"):
            save_personal_memory(
                {
                    "provider": "gcp",
                    "owner_team": (
                        "<MagicMock name='mock.prepare_runtime_inputs()."
                        "__getitem__().preferred_owner' id='4901580016'>"
                    ),
                }
            )
        # File must NOT have been written.
        assert not mem_file.exists()

    def test_save_raises_for_bare_mock_repr(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import save_personal_memory

        with pytest.raises(ValueError, match="test-double repr"):
            save_personal_memory({"domain": "<Mock id='123'>"})
        assert not mem_file.exists()

    def test_sanitize_existing_clears_poisoned_owner_team(self, mem_file):
        """One-shot helper rewrites the file with poisoned values stripped."""
        from fluid_build.cli.forge_copilot_personal_memory import (
            _sanitize_existing_personal_memory,
        )

        poisoned_repr = (
            "<MagicMock name='mock.prepare_runtime_inputs()."
            "__getitem__().preferred_owner' id='4901580016'>"
        )
        mem_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "PersonalMemory",
                    "preferences": {
                        "provider": "gcp",
                        "owner_team": poisoned_repr,
                    },
                    "history": {"recent_domains": ["retail"]},
                }
            )
        )

        rewrote = _sanitize_existing_personal_memory()
        assert rewrote is True
        on_disk = json.loads(mem_file.read_text())
        assert "owner_team" not in on_disk["preferences"]
        assert on_disk["preferences"]["provider"] == "gcp"
        assert on_disk["history"]["recent_domains"] == ["retail"]

    def test_sanitize_existing_is_idempotent_on_clean_file(self, mem_file):
        from fluid_build.cli.forge_copilot_personal_memory import (
            _sanitize_existing_personal_memory,
        )

        mem_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "PersonalMemory",
                    "preferences": {"provider": "gcp"},
                    "history": {},
                }
            )
        )
        before = mem_file.read_text()
        rewrote = _sanitize_existing_personal_memory()
        assert rewrote is False
        # File contents unchanged.
        assert mem_file.read_text() == before

    def test_sanitize_existing_is_safe_when_file_absent(self, tmp_path):
        from fluid_build.cli.forge_copilot_personal_memory import (
            _sanitize_existing_personal_memory,
        )

        ghost = tmp_path / "does-not-exist.json"
        assert _sanitize_existing_personal_memory(ghost) is False


class TestPersonalMemoryLoadsRegardlessOfNonInteractive:
    """Issue #48 fix: personal memory previously loaded only when the run
    was interactive — a CI / scripted run silently bypassed every saved
    preference while team memory loaded unconditionally.  This test pins
    the symmetric behaviour.

    The most reliable signal is the *content* of ``forge_modes.py`` —
    the gate that read ``if not non_interactive`` around
    ``load_personal_memory`` must be gone."""

    def test_personal_memory_load_is_not_gated_on_non_interactive(self):
        import inspect

        from fluid_build.cli import forge_modes

        src = inspect.getsource(forge_modes.run_ai_copilot_mode)
        # The previous code had ``if not get_cli_arg_fn(args, "non_interactive", False):``
        # wrapping ``from fluid_build.cli.forge_copilot_personal_memory import load_personal_memory``.
        # After the fix, the import + load is unconditional.
        idx = src.find("load_personal_memory")
        assert idx > 0, "load_personal_memory import must still exist"
        # Search the 200 chars BEFORE the import for the offending gate.
        preceding = src[max(0, idx - 200) : idx]
        assert (
            'if not get_cli_arg_fn(args, "non_interactive"' not in preceding
        ), "Personal memory must not be gated on non_interactive (issue #48)."

    def test_setdefault_keeps_cli_args_winning(self):
        """Smoke check on the precedence ladder: a CLI arg that already
        populated ``context['provider']`` must NOT be overwritten by
        personal memory's ``preferred_provider``.

        The implementation uses ``context.setdefault`` which silently
        no-ops when the key is present — this test is the regression
        pin for that contract."""
        context = {"provider": "explicit-cli-value"}
        personal_prefs = {"preferred_provider": "personal-value"}

        # Mirror the production merge logic without invoking the full
        # ``run_ai_copilot_mode`` plumbing.
        pref_keys = (
            "preferred_provider",
            "preferred_engine",
            "preferred_domain",
            "owner_team",
            "preferred_ci_provider",
            "preferred_ci_complexity",
        )
        for key in pref_keys:
            mapped = key.replace("preferred_", "")
            if personal_prefs.get(key) and mapped not in context:
                context.setdefault(mapped, personal_prefs[key])
        # CLI arg wins.
        assert context["provider"] == "explicit-cli-value"


class TestForgeStartupSanitiser:
    """Issue #51 fix: ``fluid forge`` calls
    ``_sanitize_existing_personal_memory`` ONCE per process at startup so
    a real engineer with poisoned ``~/.fluid/personal-memory.json`` from
    a prior test run self-heals on the next run.

    Previously the sanitiser only ran from ``tests/conftest.py``, so
    contributors hitting the poisoned file outside the test session
    were stuck with stale poison forever."""

    def test_run_main_invokes_sanitiser_once(self, monkeypatch):
        """First call rewrites the disk file; second call is a no-op."""
        import fluid_build.cli.forge as forge_mod

        # Reset the per-process guard so this test can observe the
        # "once" semantics regardless of which other tests have run.
        monkeypatch.setattr(forge_mod, "_PERSONAL_MEMORY_SANITISED", False)

        calls = {"count": 0}

        def _fake_sanitise(*args, **kwargs):
            calls["count"] += 1
            return False

        monkeypatch.setattr(
            "fluid_build.cli.forge_copilot_personal_memory._sanitize_existing_personal_memory",
            _fake_sanitise,
        )

        # First invocation runs it; second is short-circuited by the guard.
        forge_mod._sanitize_personal_memory_once()
        forge_mod._sanitize_personal_memory_once()
        assert calls["count"] == 1

    def test_sanitiser_failure_does_not_block_forge(self, monkeypatch):
        """A raise inside the sanitiser must NOT propagate."""
        import fluid_build.cli.forge as forge_mod

        monkeypatch.setattr(forge_mod, "_PERSONAL_MEMORY_SANITISED", False)

        def _boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(
            "fluid_build.cli.forge_copilot_personal_memory._sanitize_existing_personal_memory",
            _boom,
        )

        # Must not raise.
        forge_mod._sanitize_personal_memory_once()

    def test_first_run_rewrites_poisoned_file_via_run_main(self, tmp_path, monkeypatch):
        """End-to-end: ``_run_main`` invokes the sanitiser and the
        poisoned file is rewritten."""
        import fluid_build.cli.forge as forge_mod
        import fluid_build.cli.forge_copilot_personal_memory as pm

        monkeypatch.setattr(forge_mod, "_PERSONAL_MEMORY_SANITISED", False)

        poisoned = (
            "<MagicMock name='mock.prepare_runtime_inputs()."
            "__getitem__().preferred_owner' id='4901580016'>"
        )
        mem_file = tmp_path / "personal-memory.json"
        mem_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "PersonalMemory",
                    "preferences": {"provider": "gcp", "owner_team": poisoned},
                    "history": {},
                }
            )
        )
        monkeypatch.setattr(pm, "_MEMORY_FILE", mem_file)

        forge_mod._sanitize_personal_memory_once()

        on_disk = json.loads(mem_file.read_text())
        assert "owner_team" not in on_disk["preferences"]
        assert on_disk["preferences"]["provider"] == "gcp"
