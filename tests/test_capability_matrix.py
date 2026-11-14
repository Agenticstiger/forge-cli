# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The capability matrix (providers, templates, build engines) and its process cache.

Command Center imports ``build_capability_matrix`` from
``fluid_build.cli.forge_copilot_runtime``, which re-exports it from
``fluid_build.cli._capability_matrix``; both paths must resolve to one function.
"""

from __future__ import annotations

from fluid_build.cli import _capability_matrix as matrix_module
from fluid_build.cli.forge_copilot_runtime import (
    build_capability_matrix,
    clear_capability_matrix_cache,
)


def test_command_center_import_path_is_the_new_module() -> None:
    assert build_capability_matrix is matrix_module.build_capability_matrix
    assert clear_capability_matrix_cache is matrix_module.clear_capability_matrix_cache


class TestCapabilityMatrixCache:
    def setup_method(self):
        clear_capability_matrix_cache()

    def teardown_method(self):
        clear_capability_matrix_cache()

    def test_first_call_returns_a_matrix(self):
        matrix = build_capability_matrix()
        assert isinstance(matrix, dict)
        assert "providers" in matrix
        assert "templates" in matrix
        assert "build_engines" in matrix
        assert "provider_engine_compatibility" in matrix

    def test_second_call_returns_equivalent_matrix(self):
        assert build_capability_matrix() == build_capability_matrix()

    def test_caller_mutation_does_not_poison_cache(self):
        m1 = build_capability_matrix()
        m1["providers"].append("evil-fake-provider")
        m1["templates"]["evil-fake-template"] = {"description": "injected"}

        m2 = build_capability_matrix()
        assert "evil-fake-provider" not in m2["providers"]
        assert "evil-fake-template" not in m2["templates"]

    def test_clear_cache_forces_rebuild(self):
        m1 = build_capability_matrix()
        clear_capability_matrix_cache()
        m2 = build_capability_matrix()
        assert m1 == m2
        assert m1 is not m2

    def test_local_provider_supports_every_ingestion_engine(self):
        compat = build_capability_matrix()["provider_engine_compatibility"]
        assert {"duckdb", "dlt", "dbt"} <= set(compat["local"])
