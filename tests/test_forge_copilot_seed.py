# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Phase 7 — ``fluid forge --seed-from`` pre-processor tests.

The seed pre-processor (``fluid_build.cli.forge_copilot_seed``) accepts the
same three input shapes as ``fluid opds import`` — lone ODCS, single ODPS
file, or directory — and produces a structural FLUID skeleton the LLM uses
as ground truth. These tests prove each shape produces an equivalent
skeleton with the expected provenance, and that the ground-truth diff
guard catches schema mutations.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from fluid_build.cli.forge_copilot_seed import (
    SHAPE_DIRECTORY,
    SHAPE_ODCS_FILE,
    SHAPE_ODCS_ONLY_DIRECTORY,
    SHAPE_ODPS_FILE,
    diff_against_seed,
    load_seed,
    resolve_at_path,
)
from fluid_build.providers.base import ProviderError

FIXTURES = Path(__file__).parent / "fixtures"
BUNDLE_DIR = FIXTURES / "odps" / "product-bitol"
CONTRACTS_ONLY = FIXTURES / "odps" / "contracts-only"


# ---------------------------------------------------------------------------
# Shape detection + skeleton generation
# ---------------------------------------------------------------------------


class TestShapeDetection:
    def test_lone_odcs_file(self) -> None:
        odcs = next(BUNDLE_DIR.glob("*.odcs.yaml"))
        seed = load_seed(odcs)
        assert seed.shape == SHAPE_ODCS_FILE
        assert len(seed.fluid["exposes"]) == 1

    def test_single_odps_file(self) -> None:
        odps = next(BUNDLE_DIR.glob("*.odps.yaml"))
        seed = load_seed(odps)
        assert seed.shape == SHAPE_ODPS_FILE
        assert len(seed.fluid["exposes"]) == 2

    def test_directory_bundle(self) -> None:
        seed = load_seed(BUNDLE_DIR)
        assert seed.shape == SHAPE_DIRECTORY
        assert len(seed.fluid["exposes"]) == 2

    def test_directory_odcs_only(self) -> None:
        seed = load_seed(CONTRACTS_ONLY)
        assert seed.shape == SHAPE_ODCS_ONLY_DIRECTORY
        # Two ODCS files → two exposes, no product wrapper
        n_files = len(list(CONTRACTS_ONLY.glob("*.odcs.yaml")))
        assert len(seed.fluid["exposes"]) == n_files


class TestEquivalence:
    """ODPS-file and directory imports should produce the same skeleton."""

    def test_file_and_directory_seeds_are_equivalent(self) -> None:
        odps = next(BUNDLE_DIR.glob("*.odps.yaml"))
        seed_file = load_seed(odps)
        seed_dir = load_seed(BUNDLE_DIR)
        assert seed_file.fluid == seed_dir.fluid


# ---------------------------------------------------------------------------
# Provenance + missing input
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_lone_odcs_provenance_records_path(self) -> None:
        odcs = next(BUNDLE_DIR.glob("*.odcs.yaml"))
        seed = load_seed(odcs)
        assert seed.provenance[0]["origin"] == str(odcs)

    def test_directory_provenance_enumerates_odcs_files(self) -> None:
        seed = load_seed(BUNDLE_DIR)
        origins = [entry["origin"] for entry in seed.provenance]
        for child in BUNDLE_DIR.glob("*.odcs.yaml"):
            assert str(child) in origins


class TestMissingPath:
    def test_nonexistent_path_raises(self) -> None:
        with pytest.raises(ProviderError, match="not found"):
            load_seed("/does/not/exist.odps.yaml")


# ---------------------------------------------------------------------------
# Ground-truth diff guard
# ---------------------------------------------------------------------------


class TestGroundTruthGuard:
    def test_no_mutation_returns_empty_diff(self) -> None:
        seed = load_seed(BUNDLE_DIR)
        # Candidate is the seed itself — must produce zero mismatches
        assert diff_against_seed(seed, seed.fluid) == []

    def test_schema_mutation_is_detected(self) -> None:
        seed = load_seed(BUNDLE_DIR)
        mutated = copy.deepcopy(seed.fluid)
        # Remove one field from the first expose's schema — the guard must catch it
        mutated["exposes"][0]["contract"]["schema"].pop()
        mismatches = diff_against_seed(seed, mutated)
        assert any("exposes[0].contract.schema" in m["path"] for m in mismatches)

    def test_qos_mutation_is_detected(self) -> None:
        seed = load_seed(BUNDLE_DIR)
        mutated = copy.deepcopy(seed.fluid)
        # Find an expose with qos and mutate it
        for expose in mutated.get("exposes", []):
            if "qos" in expose:
                expose["qos"]["availability"] = "0%"
                break
        else:
            pytest.skip("seed fixture had no qos to mutate")
        mismatches = diff_against_seed(seed, mutated)
        assert any(".qos" in m["path"] for m in mismatches)

    def test_adding_unrelated_fields_does_not_trigger_diff(self) -> None:
        seed = load_seed(BUNDLE_DIR)
        mutated = copy.deepcopy(seed.fluid)
        # The LLM is allowed to add builds/executes/governance
        mutated["builds"] = {"transform": "SELECT * FROM source"}
        mutated["executes"] = {"schedule": "0 5 * * *"}
        mismatches = diff_against_seed(seed, mutated)
        assert mismatches == []


# ---------------------------------------------------------------------------
# Path resolver helper
# ---------------------------------------------------------------------------


class TestResolveAtPath:
    def test_resolves_array_index(self) -> None:
        data = {"exposes": [{"id": "a"}, {"id": "b"}]}
        assert resolve_at_path(data, "exposes[1].id") == "b"

    def test_returns_none_for_missing_path(self) -> None:
        assert resolve_at_path({}, "exposes[0].id") is None
