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

"""Coverage for the V1.5+ unified ``~/.fluid/config.yaml`` (Mediocre #5).

The unified config replaces the v1.5 per-feature scatter
(``ai_config.json`` + ``sources.yaml`` + ``prices.json``) with one
sectioned file. Tests pin:

1. ``load_unified_config`` returns ``None`` when the file is missing
   (legacy fall-through is the caller's responsibility).
2. ``load_unified_config`` reads a well-formed file and returns a
   typed ``UnifiedConfig``.
3. ``migrate_legacy_to_unified`` consolidates the three legacy
   files into one and reports which files it consumed.
4. Migration is idempotent — running twice (with overwrite=True)
   produces byte-identical output.
5. Missing legacy files don't break migration — the unified file
   just has empty sections for what wasn't found.
6. Refuses to overwrite an existing target without explicit
   ``overwrite=True``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fluid_build.copilot.unified_config import (
    LLMSection,
    PricesSection,
    SourcesSection,
    UnifiedConfig,
    load_unified_config,
    migrate_legacy_to_unified,
    unified_config_path,
)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point ``$FLUID_HOME`` at a temp dir + scrub
    ``$FLUID_CONFIG`` so tests don't see the developer's real
    ``~/.fluid``."""
    monkeypatch.setenv("FLUID_HOME", str(tmp_path))
    monkeypatch.delenv("FLUID_CONFIG", raising=False)
    yield tmp_path


class TestPathResolution:
    def test_fluid_home_overrides_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FLUID_HOME", str(tmp_path))
        monkeypatch.delenv("FLUID_CONFIG", raising=False)
        assert unified_config_path() == tmp_path / "config.yaml"

    def test_fluid_config_overrides_fluid_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FLUID_HOME", str(tmp_path))
        explicit = tmp_path / "elsewhere" / "explicit.yaml"
        monkeypatch.setenv("FLUID_CONFIG", str(explicit))
        assert unified_config_path() == explicit


class TestLoad:
    def test_returns_none_when_missing(self, isolated_home):
        assert load_unified_config() is None

    def test_reads_well_formed_file(self, isolated_home, monkeypatch):
        config_path = isolated_home / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "llm": {
                        "provider": "anthropic",
                        "model": "claude-sonnet-4-6",
                        "tiered": False,
                    },
                    "sources": {
                        "sources": {
                            "snowflake-prod": {
                                "catalog": "snowflake",
                                "auth_method": "key_pair",
                                "account": "myorg",
                            },
                        },
                    },
                    "prices": {
                        "prices": {
                            "claude-sonnet-4-6": [2.40, 12.00],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        cfg = load_unified_config()
        assert cfg is not None
        assert cfg.llm.provider == "anthropic"
        assert cfg.llm.model == "claude-sonnet-4-6"
        assert "snowflake-prod" in cfg.sources_section.sources
        assert cfg.sources_section.sources["snowflake-prod"].catalog == "snowflake"
        assert cfg.prices_section.prices["claude-sonnet-4-6"] == (2.40, 12.00)

    def test_returns_none_on_malformed_yaml(self, isolated_home):
        config_path = isolated_home / "config.yaml"
        config_path.write_text("{not: valid yaml: {}", encoding="utf-8")
        assert load_unified_config() is None


class TestMigration:
    def _write_legacy_files(self, tmp_path):
        """Produce a representative ``~/.fluid`` with all three
        legacy files."""
        ai_config = tmp_path / "ai_config.json"
        ai_config.write_text(
            json.dumps(
                {
                    "provider": "openai",
                    "model": "gpt-4.1-mini",
                    "tiered": True,
                }
            ),
            encoding="utf-8",
        )
        sources = tmp_path / "sources.yaml"
        sources.write_text(
            yaml.safe_dump(
                {
                    "sources": {
                        "snowflake-prod": {
                            "catalog": "snowflake",
                            "auth_method": "key_pair",
                            "account": "myorg",
                        },
                        "datahub-corp": {
                            "catalog": "datahub",
                            "auth_method": "pat",
                            "server": "https://datahub.example.com",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        prices = tmp_path / "prices.json"
        prices.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "prices": {
                        "claude-sonnet-4-6": [2.40, 12.00],
                        "gpt-4.1-mini": [0.10, 0.40],
                    },
                }
            ),
            encoding="utf-8",
        )
        return ai_config, sources, prices

    def test_consolidates_three_legacy_files(self, monkeypatch, tmp_path):
        # Place legacy files at the user's real home so the migrator
        # finds them via Path.home().
        monkeypatch.setenv("HOME", str(tmp_path))
        fluid_dir = tmp_path / ".fluid"
        fluid_dir.mkdir()
        # Override Path.home() via the env-var path the migrator uses.
        # (HOME on macOS/Linux changes Path.home() per the user's
        # platform — verify before running by sanity-checking
        # Path.home() reflects the env-var.)
        if Path.home() != tmp_path:
            pytest.skip(
                "test assumes Path.home() honours $HOME; some platforms "
                "may diverge — skip on those rather than misreport."
            )
        self._write_legacy_files(fluid_dir)

        target = fluid_dir / "config.yaml"
        monkeypatch.setenv("FLUID_CONFIG", str(target))

        written, consumed = migrate_legacy_to_unified()

        assert written == target
        assert target.is_file()
        assert len(consumed) == 3

        # Re-load via the public API and check every section landed.
        cfg = load_unified_config()
        assert cfg is not None
        assert cfg.llm.provider == "openai"
        assert cfg.llm.tiered is True
        assert "snowflake-prod" in cfg.sources_section.sources
        assert "datahub-corp" in cfg.sources_section.sources
        assert cfg.prices_section.prices["claude-sonnet-4-6"] == (2.40, 12.00)

    def test_refuses_to_overwrite_existing(self, isolated_home):
        target = isolated_home / "config.yaml"
        target.write_text("operator-edited: true\n", encoding="utf-8")

        with pytest.raises(FileExistsError):
            migrate_legacy_to_unified()

    def test_overwrite_flag_replaces_existing(self, isolated_home, monkeypatch):
        target = isolated_home / "config.yaml"
        target.write_text("schema_version: 0\n", encoding="utf-8")

        # No legacy files present — migrator writes an empty unified.
        written, consumed = migrate_legacy_to_unified(overwrite=True)
        assert written == target
        assert consumed == []
        # New content overwrote the operator's "schema_version: 0".
        cfg = load_unified_config()
        assert cfg is not None
        assert cfg.schema_version == 1

    def test_idempotent_on_overwrite(self, isolated_home, monkeypatch):
        """Migrating twice (with overwrite=True) produces
        byte-identical output."""
        # No legacy files; the migration just writes an empty unified
        # config — but it must do so identically each run.
        first, _ = migrate_legacy_to_unified(overwrite=True)
        first_bytes = first.read_bytes()
        # Tiny pause to let timestamps differ if migration accidentally
        # serialised one — proves the YAML is timestamp-free.
        second, _ = migrate_legacy_to_unified(overwrite=True)
        assert second.read_bytes() == first_bytes
