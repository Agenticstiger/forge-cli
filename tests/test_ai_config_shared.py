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

"""Tests for the shared AI-config leaf (``cli/_ai_config_shared.py``).

Covers two things:

1. **The cycle is gone.** ``cli.ai_setup`` and ``cli.forge_copilot_llm_providers``
   import cleanly in either order (fresh interpreters, both directions), and the
   leaf itself back-imports neither module (nor the storage layer). This is the
   regression guard for the ``refactor(forge): break ai_setup <-> llm_providers
   circular import`` change.
2. **Behaviour is preserved.** The path-injected loaders parse both the v2
   multi-provider map and the legacy single-provider shape exactly as the old
   ``_ai_setup_storage`` readers did, and the storage/ai_setup shims still
   delegate to them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from fluid_build.cli import _ai_config_shared

# ---------------------------------------------------------------------------
# (1) No circular import — fresh interpreters, both orders
# ---------------------------------------------------------------------------


def _fresh_import(code: str) -> subprocess.CompletedProcess:
    """Run *code* in a brand-new interpreter that can import ``fluid_build``.

    ``PYTHONPATH`` is seeded from the parent's ``sys.path`` so the subprocess
    resolves the package whether fluid is pip-installed (CI) or run from a
    source checkout via ``PYTHONPATH`` (local worktree).
    """
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.parametrize(
    "first, second",
    [
        ("fluid_build.cli.ai_setup", "fluid_build.cli.forge_copilot_llm_providers"),
        ("fluid_build.cli.forge_copilot_llm_providers", "fluid_build.cli.ai_setup"),
    ],
)
def test_no_circular_import_in_either_order(first, second):
    code = f"import {first}\nimport {second}\nprint('OK')"
    result = _fresh_import(code)
    assert result.returncode == 0, f"import cycle regressed:\n{result.stderr}"
    assert "OK" in result.stdout


def test_leaf_is_tier_zero_no_cycle_backedges():
    """Importing the leaf must not pull either cycle participant or the storage layer."""
    code = (
        "import sys\n"
        "import fluid_build.cli._ai_config_shared\n"
        "cycle = ('fluid_build.cli.ai_setup', "
        "'fluid_build.cli.forge_copilot_llm_providers', "
        "'fluid_build.cli._ai_setup_storage')\n"
        "bad = [m for m in cycle if m in sys.modules]\n"
        "print('BAD=' + ','.join(bad))\n"
    )
    result = _fresh_import(code)
    assert result.returncode == 0, result.stderr
    assert "BAD=" in result.stdout
    # Nothing after the ``=`` -> no cycle module was pulled in.
    tail = result.stdout.split("BAD=", 1)[1].strip()
    assert tail == "", f"leaf back-imported a cycle module: {tail}"


def test_check_llm_readiness_no_longer_imports_ai_setup():
    """The reroute means importing llm_providers must not eagerly pull ai_setup."""
    code = (
        "import sys\n"
        "import fluid_build.cli.forge_copilot_llm_providers\n"
        "print('AISETUP=' + str('fluid_build.cli.ai_setup' in sys.modules))\n"
    )
    result = _fresh_import(code)
    assert result.returncode == 0, result.stderr
    assert "AISETUP=False" in result.stdout


# ---------------------------------------------------------------------------
# (2) Behaviour preserved — path-injected loaders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_unified_config(monkeypatch):
    """Neutralise the ``~/.fluid/config.yaml`` unified-config override.

    ``load_ai_config`` consults unified config first; stub it to ``None`` so the
    ``ai_config.json`` read path is exercised deterministically regardless of the
    developer's real home directory.
    """
    import fluid_build.copilot.unified_config as uc

    monkeypatch.setattr(uc, "load_unified_config", lambda *a, **k: None)


def _write(tmp_path, payload: dict):
    cf = tmp_path / "ai_config.json"
    cf.write_text(json.dumps(payload), encoding="utf-8")
    return cf


def test_load_ai_config_v2_active_entry(tmp_path):
    cf = _write(
        tmp_path,
        {
            "version": 2,
            "active": "openai",
            "providers": {
                "openai": {"model": "gpt-4o"},
                "gemini": {"model": "gemini-2.5-pro"},
            },
        },
    )
    loaded = _ai_config_shared.load_ai_config(cf)
    assert loaded == {"provider": "openai", "model": "gpt-4o"}


def test_load_ai_config_legacy_single_provider(tmp_path):
    cf = _write(tmp_path, {"provider": "anthropic", "model": "claude-haiku-4-5"})
    loaded = _ai_config_shared.load_ai_config(cf)
    assert loaded == {"provider": "anthropic", "model": "claude-haiku-4-5"}


def test_load_ai_config_missing_file_returns_none(tmp_path):
    assert _ai_config_shared.load_ai_config(tmp_path / "absent.json") is None


def test_load_ai_config_map_flattens_entries(tmp_path):
    cf = _write(
        tmp_path,
        {
            "version": 2,
            "active": "gemini",
            "providers": {"gemini": {"model": "g"}, "openai": {"model": "o"}},
        },
    )
    cfg_map = _ai_config_shared.load_ai_config_map(cf)
    assert cfg_map["active"] == "gemini"
    assert cfg_map["providers"]["openai"] == {"provider": "openai", "model": "o"}


def test_load_ai_config_for_specific_provider(tmp_path):
    cf = _write(
        tmp_path,
        {
            "version": 2,
            "active": "openai",
            "providers": {"openai": {"model": "o"}, "gemini": {"model": "g"}},
        },
    )
    assert _ai_config_shared.load_ai_config_for("gemini", cf) == {
        "provider": "gemini",
        "model": "g",
    }
    assert _ai_config_shared.load_ai_config_for("anthropic", cf) is None
    assert _ai_config_shared.load_ai_config_for("", cf) is None


def test_list_configured_providers_sorted(tmp_path):
    cf = _write(
        tmp_path,
        {
            "version": 2,
            "active": "openai",
            "providers": {"openai": {}, "gemini": {}, "anthropic": {}},
        },
    )
    assert _ai_config_shared.list_configured_providers(cf) == [
        "anthropic",
        "gemini",
        "openai",
    ]


def test_normalize_config_handles_junk():
    assert _ai_config_shared._normalize_config(None) == {"active": None, "providers": {}}
    assert _ai_config_shared._normalize_config({"garbage": 1}) == {
        "active": None,
        "providers": {},
    }


def test_default_config_file_falls_back_when_no_path(monkeypatch, tmp_path):
    """A ``None`` config_file uses the leaf's own ``_CONFIG_FILE`` default."""
    cf = _write(tmp_path, {"provider": "openai", "model": "gpt-4o"})
    monkeypatch.setattr(_ai_config_shared, "_CONFIG_FILE", cf)
    assert _ai_config_shared.load_ai_config() == {"provider": "openai", "model": "gpt-4o"}


# ---------------------------------------------------------------------------
# (2b) Shims still resolve — ai_setup constants + storage delegation
# ---------------------------------------------------------------------------


def test_ai_setup_reexports_leaf_constants():
    from fluid_build.cli import ai_setup

    assert ai_setup._CONFIG_DIR is _ai_config_shared._CONFIG_DIR
    assert ai_setup._CONFIG_FILE is _ai_config_shared._CONFIG_FILE


def test_storage_load_delegates_to_leaf(monkeypatch, tmp_path):
    """``_ai_setup_storage._load_ai_config`` reads through the leaf, honouring
    the ``ai_setup._CONFIG_FILE`` indirection seam."""
    from fluid_build.cli import _ai_setup_storage, ai_setup

    cf = _write(tmp_path, {"provider": "gemini", "model": "gemini-2.5-pro"})
    monkeypatch.setattr(ai_setup, "_CONFIG_FILE", cf)
    assert _ai_setup_storage._load_ai_config() == {
        "provider": "gemini",
        "model": "gemini-2.5-pro",
    }
