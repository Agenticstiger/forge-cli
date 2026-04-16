# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Tests for fluid_build.cli.artifact_paths — the central path registry."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from fluid_build.cli import artifact_paths as ap


class TestUserScope:
    """User-level paths under ``~/.fluid/``."""

    def test_user_fluid_dir_defaults_to_home(self):
        expected = Path.home() / ".fluid"
        assert ap.user_fluid_dir() == expected

    def test_user_fluid_dir_honors_fluid_home_env_override(self, tmp_path):
        with patch.dict(os.environ, {"FLUID_HOME": str(tmp_path / "custom")}):
            assert ap.user_fluid_dir() == tmp_path / "custom"

    def test_user_personal_memory_path_uses_new_filename(self):
        """Slice 5 target path (not the legacy engineer_memory.json)."""
        assert ap.user_personal_memory_path().name == "personal-memory.json"

    def test_user_config_path_uses_yaml(self):
        assert ap.user_config_path().name == "config.yaml"


class TestWorkspaceScope:
    """Workspace-level paths under ``<ws>/`` and ``<ws>/.fluid/``."""

    def test_workspace_config_filename_matches_shipping_name(self):
        """Backward compat — this file is committed in every repo."""
        assert ap.WORKSPACE_CONFIG_FILENAME == "fluid.workspace.yaml"

    def test_workspace_state_dir_is_hidden(self):
        assert ap.WORKSPACE_STATE_DIRNAME == ".fluid"
        ws = Path("/ws")
        assert ap.workspace_state_dir(ws) == ws / ".fluid"

    def test_workspace_skills_path(self):
        ws = Path("/ws")
        assert ap.workspace_skills_path(ws) == ws / ".fluid" / "skills.yaml"

    def test_workspace_init_receipt_path(self):
        ws = Path("/ws")
        assert (
            ap.workspace_init_receipt_path(ws)
            == ws / ".fluid" / "init-receipt.json"
        )


class TestProductScope:
    """Product-level paths under ``<ws>/<product>/``."""

    def test_contract_filename_unchanged(self):
        """The contract file is the product identity marker."""
        assert ap.CONTRACT_FILENAME == "contract.fluid.yaml"

    def test_bundled_and_lock_are_product_root_siblings(self):
        """Bundled + lockfile live next to the contract, not hidden."""
        p = Path("/ws/my-product")
        assert ap.product_contract_path(p) == p / "contract.fluid.yaml"
        assert ap.product_bundled_path(p) == p / "contract.bundled.yaml"
        assert ap.product_lock_path(p) == p / "contract.lock.yaml"

    def test_fragments_and_overlays_at_product_root(self):
        p = Path("/ws/my-product")
        assert ap.product_fragments_dir(p) == p / "fragments"
        assert ap.product_overlays_dir(p) == p / "overlays"

    def test_state_dir_is_hidden(self):
        p = Path("/ws/my-product")
        assert ap.product_state_dir(p) == p / ".fluid"

    def test_per_product_state_files_live_under_dot_fluid(self):
        p = Path("/ws/my-product")
        assert ap.product_memory_path(p) == p / ".fluid" / "copilot-memory.json"
        assert ap.product_ci_state_path(p) == p / ".fluid" / "ci-state.json"
        assert (
            ap.product_forge_receipt_path(p)
            == p / ".fluid" / "forge-receipt.json"
        )


class TestEnvelopeAndCIConstants:
    def test_envelope_schema_version_is_int(self):
        assert isinstance(ap.ENVELOPE_SCHEMA_VERSION, int)
        assert ap.ENVELOPE_SCHEMA_VERSION >= 1

    def test_ci_header_schema_carries_version_suffix(self):
        assert ap.CI_HEADER_SCHEMA == "fluid.ci/v1"


class TestNoLegacyHelpers:
    """The clean-cut directive: no legacy fallbacks exported from the registry."""

    def test_no_legacy_personal_memory_filename_exported(self):
        assert not hasattr(ap, "LEGACY_PERSONAL_MEMORY_FILENAME")

    def test_no_legacy_personal_memory_path_helper(self):
        assert not hasattr(ap, "legacy_personal_memory_path")

    def test_no_legacy_run_state_dirname(self):
        assert not hasattr(ap, "LEGACY_RUN_STATE_DIRNAME")

    def test_no_legacy_product_memory_path_helper(self):
        assert not hasattr(ap, "legacy_product_memory_path")


class TestAcceptsStringsAndPaths:
    """Every helper should accept both str and Path for its root argument."""

    def test_workspace_state_dir_accepts_string(self):
        assert ap.workspace_state_dir("/ws") == Path("/ws") / ".fluid"

    def test_product_memory_path_accepts_string(self):
        assert (
            ap.product_memory_path("/ws/p")
            == Path("/ws/p") / ".fluid" / "copilot-memory.json"
        )

    def test_product_bundled_path_accepts_string(self):
        assert (
            ap.product_bundled_path("/ws/p")
            == Path("/ws/p") / "contract.bundled.yaml"
        )
