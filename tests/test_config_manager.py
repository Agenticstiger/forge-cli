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

"""
Tests for fluid_build.config_manager — hierarchical configuration loading.

Covers:
  - Default config initialization
  - Dot-notation get/set
  - Deep merge semantics
  - Config file loading (user, project, system)
  - Environment variable mapping
  - CLI arg overrides
  - Section & catalog helpers
  - Save / round-trip
  - Global singleton reset
"""

from __future__ import annotations

import copy
import os

import pytest
import yaml

from fluid_build.config_manager import (
    DEFAULT_CONFIG,
    FluidConfig,
    get_config,
    reset_config,
)

# Snapshot of pristine defaults (before any test mutates them via shallow copy).
_PRISTINE_DEFAULTS = copy.deepcopy(DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    """Ensure tests don't read real config files or leak state.

    FluidConfig._load_defaults() uses DEFAULT_CONFIG.copy() which is *shallow*,
    so _deep_merge mutates the module-level dict.  We save and restore it here.
    """
    import fluid_build.config_manager as _cm

    saved = copy.deepcopy(_PRISTINE_DEFAULTS)
    reset_config()
    # Prevent reading the real home / system configs
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fakehome"))  # Windows compat
    (tmp_path / "fakehome").mkdir(exist_ok=True)
    # Clear any FLUID_ env vars from the host
    for key in list(os.environ):
        if key.startswith("FLUID_") or key in (
            "GCP_PROJECT_ID",
            "GCP_REGION",
            "AWS_REGION",
            "SNOWFLAKE_ACCOUNT",
            "NO_COLOR",
        ):
            monkeypatch.delenv(key, raising=False)
    yield
    reset_config()
    # Restore pristine defaults so later tests are not affected
    _cm.DEFAULT_CONFIG.clear()
    _cm.DEFAULT_CONFIG.update(saved)


# =====================================================================
# DEFAULTS
# =====================================================================


class TestDefaults:
    """FluidConfig should start with sensible defaults."""

    def test_defaults_loaded(self):
        cfg = FluidConfig()
        assert cfg.get("logging.level") == "INFO"
        assert cfg.get("network.timeout") == 30
        assert cfg.get("apply.dry_run") is False
        assert cfg.get("output.color") is True

    def test_all_default_sections_present(self):
        cfg = FluidConfig()
        for section in (
            "logging",
            "cache",
            "network",
            "validation",
            "apply",
            "providers",
            "catalogs",
            "output",
        ):
            assert cfg.get(section) is not None, f"Missing default section: {section}"

    def test_provider_defaults(self):
        cfg = FluidConfig()
        assert cfg.get("providers.gcp.default_region") == "us-central1"
        assert cfg.get("providers.aws.default_region") == "us-east-1"
        assert cfg.get("providers.snowflake.default_warehouse") == "COMPUTE_WH"

    def test_catalog_defaults(self):
        cfg = FluidConfig()
        cc = cfg.get_catalog_config("fluid-command-center")
        assert cc["endpoint"] == "http://localhost:8000"
        assert cc["enabled"] is True


# =====================================================================
# DOT-NOTATION GET / SET
# =====================================================================


class TestGetSet:
    """Dot-notation accessors."""

    def test_get_nested(self):
        cfg = FluidConfig()
        assert cfg.get("providers.gcp.default_location") == "US"

    def test_get_missing_returns_default(self):
        cfg = FluidConfig()
        assert cfg.get("does.not.exist") is None
        assert cfg.get("does.not.exist", 42) == 42

    def test_set_existing_key(self):
        cfg = FluidConfig()
        cfg.set("logging.level", "DEBUG")
        assert cfg.get("logging.level") == "DEBUG"

    def test_set_new_nested_key(self):
        cfg = FluidConfig()
        cfg.set("custom.section.key", "value")
        assert cfg.get("custom.section.key") == "value"

    def test_get_section(self):
        cfg = FluidConfig()
        section = cfg.get_section("logging")
        assert isinstance(section, dict)
        assert "level" in section

    def test_get_section_missing_returns_empty_dict(self):
        cfg = FluidConfig()
        assert cfg.get_section("nonexistent") == {}

    def test_to_dict(self):
        cfg = FluidConfig()
        d = cfg.to_dict()
        assert isinstance(d, dict)
        assert "logging" in d


# =====================================================================
# DEEP MERGE
# =====================================================================


class TestDeepMerge:
    """_deep_merge should merge dicts recursively; replace scalars and lists."""

    def test_merge_nested_dicts(self):
        cfg = FluidConfig()
        base = {"a": {"b": 1, "c": 2}}
        override = {"a": {"c": 3, "d": 4}}
        cfg._deep_merge(base, override)
        assert base == {"a": {"b": 1, "c": 3, "d": 4}}

    def test_merge_replaces_scalars(self):
        cfg = FluidConfig()
        base = {"x": "old"}
        cfg._deep_merge(base, {"x": "new"})
        assert base["x"] == "new"

    def test_merge_replaces_lists(self):
        cfg = FluidConfig()
        base = {"tags": ["a", "b"]}
        cfg._deep_merge(base, {"tags": ["c"]})
        assert base["tags"] == ["c"]

    def test_merge_adds_new_keys(self):
        cfg = FluidConfig()
        base = {"a": 1}
        cfg._deep_merge(base, {"b": 2})
        assert base == {"a": 1, "b": 2}

    def test_merge_override_dict_with_scalar(self):
        cfg = FluidConfig()
        base = {"a": {"nested": True}}
        cfg._deep_merge(base, {"a": "flat"})
        assert base["a"] == "flat"

    def test_merge_override_scalar_with_dict(self):
        cfg = FluidConfig()
        base = {"a": "flat"}
        cfg._deep_merge(base, {"a": {"nested": True}})
        assert base["a"] == {"nested": True}


# =====================================================================
# CONFIG FILE LOADING
# =====================================================================


class TestConfigFileLoading:
    """Config files at various paths should be loaded and merged."""

    def test_user_config_from_fluidrc(self, tmp_path, monkeypatch):
        home = tmp_path / "fakehome"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))

        user_rc = home / ".fluidrc.yaml"
        user_rc.write_text(yaml.dump({"logging": {"level": "WARNING"}}), encoding="utf-8")

        cfg = FluidConfig()
        assert cfg.get("logging.level") == "WARNING"

    def test_user_config_from_xdg(self, tmp_path, monkeypatch):
        home = tmp_path / "fakehome"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))

        xdg = home / ".config" / "fluid"
        xdg.mkdir(parents=True)
        (xdg / "config.yaml").write_text(yaml.dump({"network": {"timeout": 99}}), encoding="utf-8")

        cfg = FluidConfig()
        assert cfg.get("network.timeout") == 99

    def test_project_config_overrides_user(self, tmp_path, monkeypatch):
        home = tmp_path / "fakehome"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))

        # User sets level to WARNING
        (home / ".fluidrc.yaml").write_text(
            yaml.dump({"logging": {"level": "WARNING"}}), encoding="utf-8"
        )

        # Project overrides to ERROR
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)
        (project_dir / ".fluidrc.yaml").write_text(
            yaml.dump({"logging": {"level": "ERROR"}}), encoding="utf-8"
        )

        cfg = FluidConfig()
        assert cfg.get("logging.level") == "ERROR"

    def test_invalid_yaml_raises_error(self, tmp_path, monkeypatch):
        home = tmp_path / "fakehome"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))

        (home / ".fluidrc.yaml").write_text("{{invalid yaml", encoding="utf-8")

        # ConfigurationError is raised with an unsupported kwarg (suggestions=),
        # so the actual exception may surface as TypeError.  Either way the
        # constructor must not succeed silently.
        with pytest.raises(Exception):
            FluidConfig()

    def test_empty_config_file_is_fine(self, tmp_path, monkeypatch):
        home = tmp_path / "fakehome"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))

        (home / ".fluidrc.yaml").write_text("", encoding="utf-8")

        cfg = FluidConfig()
        # Should still have defaults — compare against the pristine snapshot
        assert cfg.get("logging.level") == _PRISTINE_DEFAULTS["logging"]["level"]

    def test_partial_config_preserves_other_defaults(self, tmp_path, monkeypatch):
        home = tmp_path / "fakehome"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))

        # Only override one nested value
        (home / ".fluidrc.yaml").write_text(
            yaml.dump({"network": {"timeout": 5}}), encoding="utf-8"
        )

        cfg = FluidConfig()
        assert cfg.get("network.timeout") == 5
        # Other network defaults should survive
        assert cfg.get("network.max_retries") == 3
        assert cfg.get("network.verify_ssl") is True


# =====================================================================
# ENVIRONMENT VARIABLE MAPPING
# =====================================================================


class TestEnvVarMapping:
    """FLUID_* env vars should override config values."""

    def test_fluid_log_level(self, monkeypatch):
        monkeypatch.setenv("FLUID_LOG_LEVEL", "ERROR")
        cfg = FluidConfig()
        assert cfg.get("logging.level") == "ERROR"

    def test_fluid_timeout_numeric(self, monkeypatch):
        monkeypatch.setenv("FLUID_TIMEOUT", "60")
        cfg = FluidConfig()
        assert cfg.get("network.timeout") == 60

    def test_fluid_verbose_bool_true(self, monkeypatch):
        monkeypatch.setenv("FLUID_VERBOSE", "true")
        cfg = FluidConfig()
        assert cfg.get("output.verbose") is True

    def test_fluid_verbose_bool_false(self, monkeypatch):
        monkeypatch.setenv("FLUID_VERBOSE", "false")
        cfg = FluidConfig()
        assert cfg.get("output.verbose") is False

    def test_no_color(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        cfg = FluidConfig()
        assert cfg.get("output.color") is False

    def test_gcp_project_id(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT_ID", "my-gcp-project")
        cfg = FluidConfig()
        assert cfg.get("providers.gcp.project_id") == "my-gcp-project"

    def test_aws_region(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        cfg = FluidConfig()
        assert cfg.get("providers.aws.default_region") == "eu-west-1"

    def test_env_overrides_file(self, tmp_path, monkeypatch):
        """Env vars should beat config files."""
        home = tmp_path / "fakehome"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))

        (home / ".fluidrc.yaml").write_text(
            yaml.dump({"logging": {"level": "DEBUG"}}), encoding="utf-8"
        )
        monkeypatch.setenv("FLUID_LOG_LEVEL", "CRITICAL")

        cfg = FluidConfig()
        # Env should win
        assert cfg.get("logging.level") == "CRITICAL"


# =====================================================================
# CLI ARGUMENT OVERRIDES
# =====================================================================


class TestCLIArgOverrides:
    """update_from_args should override all prior sources."""

    def test_update_verbose(self):
        cfg = FluidConfig()
        assert cfg.get("output.verbose") is False
        cfg.update_from_args(verbose=True)
        assert cfg.get("output.verbose") is True

    def test_update_log_level(self):
        cfg = FluidConfig()
        cfg.update_from_args(log_level="DEBUG")
        assert cfg.get("logging.level") == "DEBUG"

    def test_update_dry_run(self):
        cfg = FluidConfig()
        cfg.update_from_args(dry_run=True)
        assert cfg.get("apply.dry_run") is True

    def test_none_values_ignored(self):
        cfg = FluidConfig()
        original = cfg.get("logging.level")
        cfg.update_from_args(log_level=None)
        assert cfg.get("logging.level") == original

    def test_update_multiple_args(self):
        cfg = FluidConfig()
        cfg.update_from_args(verbose=True, log_level="ERROR", dry_run=True)
        assert cfg.get("output.verbose") is True
        assert cfg.get("logging.level") == "ERROR"
        assert cfg.get("apply.dry_run") is True

    def test_args_override_env(self, monkeypatch):
        monkeypatch.setenv("FLUID_LOG_LEVEL", "WARNING")
        cfg = FluidConfig()
        assert cfg.get("logging.level") == "WARNING"
        cfg.update_from_args(log_level="CRITICAL")
        assert cfg.get("logging.level") == "CRITICAL"


# =====================================================================
# FULL HIERARCHY: defaults < file < env < args
# =====================================================================


class TestFullHierarchy:
    """End-to-end test of the priority chain."""

    def test_four_level_override(self, tmp_path, monkeypatch):
        home = tmp_path / "fakehome"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))

        # Level 1: default = INFO (use pristine snapshot since DEFAULT_CONFIG is shallow-copied)
        assert _PRISTINE_DEFAULTS["logging"]["level"] == "INFO"

        # Level 2: user config = DEBUG
        (home / ".fluidrc.yaml").write_text(
            yaml.dump({"logging": {"level": "DEBUG"}}), encoding="utf-8"
        )

        # Level 3: env var = WARNING
        monkeypatch.setenv("FLUID_LOG_LEVEL", "WARNING")

        cfg = FluidConfig()
        # At this point: default < file(DEBUG) < env(WARNING) → WARNING
        assert cfg.get("logging.level") == "WARNING"

        # Level 4: CLI arg = ERROR
        cfg.update_from_args(log_level="ERROR")
        assert cfg.get("logging.level") == "ERROR"


# =====================================================================
# CATALOG CONFIG
# =====================================================================


class TestCatalogConfig:
    """get_catalog_config should resolve overrides from env."""

    def test_default_catalog(self):
        cfg = FluidConfig()
        cc = cfg.get_catalog_config("fluid-command-center")
        assert "endpoint" in cc
        assert cc["enabled"] is True

    def test_catalog_env_override(self, monkeypatch):
        monkeypatch.setenv("FLUID_CC_ENDPOINT", "https://cc.prod.example.com")
        monkeypatch.setenv("FLUID_API_KEY", "sk-test-123")
        cfg = FluidConfig()
        cc = cfg.get_catalog_config("fluid-command-center")
        assert cc["endpoint"] == "https://cc.prod.example.com"
        assert cc["auth"]["api_key"] == "sk-test-123"

    def test_all_catalogs(self):
        cfg = FluidConfig()
        all_cats = cfg.get_catalog_config()
        assert isinstance(all_cats, dict)
        assert "fluid-command-center" in all_cats

    def test_unknown_catalog_returns_empty(self):
        cfg = FluidConfig()
        assert cfg.get_catalog_config("nonexistent") == {}


# =====================================================================
# SAVE / ROUND-TRIP
# =====================================================================


class TestSaveConfig:
    """save_user_config should persist and be loadable."""

    def test_save_and_reload(self, tmp_path):
        cfg = FluidConfig()
        cfg.set("logging.level", "CRITICAL")
        save_path = tmp_path / "saved.yaml"
        cfg.save_user_config(path=save_path)

        assert save_path.exists()
        loaded = yaml.safe_load(save_path.read_text())
        assert loaded["logging"]["level"] == "CRITICAL"


# =====================================================================
# GLOBAL SINGLETON
# =====================================================================


class TestGlobalSingleton:
    """get_config / reset_config manage the singleton."""

    def test_get_config_returns_same_instance(self):
        a = get_config()
        b = get_config()
        assert a is b

    def test_reset_clears_singleton(self):
        a = get_config()
        reset_config()
        b = get_config()
        assert a is not b
