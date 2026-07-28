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

"""Combination matrix: forge ``--seed-from`` × ``opds import`` × input shapes.

Cross-product test that for every supported input shape:

  - S1: single Bitol ODPS file
  - S2: lone ODCS file
  - S3: directory bundle (ODPS + sibling ODCS files)
  - S4: ODCS-only directory (no product wrapper)

both the **import** path (``BitolOdpsProvider.import_*`` / ``OdcsProvider.
import_contract``) and the **forge seed** path
(``fluid_build.cli.forge_copilot_seed.load_seed``) produce an equivalent
FLUID skeleton — same number of exposes, same number of schema fields per
expose.

Why both: the import path is what ``fluid opds import`` runs to produce a
final FLUID contract; the seed path is what ``fluid forge --seed-from``
runs to give the LLM a structural starting point. They share most of the
underlying machinery but the seed path also computes provenance + ground-
truth paths. Drift between them is silent and catastrophic — these tests
make any drift loud.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Tuple

import pytest
import yaml

from fluid_build.cli.forge_copilot_seed import (
    SHAPE_DIRECTORY,
    SHAPE_ODCS_FILE,
    SHAPE_ODCS_ONLY_DIRECTORY,
    SHAPE_ODPS_FILE,
    diff_against_seed,
    load_seed,
)
from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odps_standard import BitolOdpsProvider

# Quiet the resolver "tried 8 paths" warning that fires for the synthetic
# ``commerce.orders-product.input.customers_raw`` input port in our fixture
# — every contract in the matrix has unresolvable input ports because we're
# not feeding their upstream contracts.
logging.getLogger("fluid_build.providers.odps_standard.provider.BitolOdpsProvider").setLevel(
    logging.ERROR
)


_FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Cross-product fixture matrix
# ---------------------------------------------------------------------------


def _render_bundle(tmp_path: Path, fluid_path: Path) -> Path:
    """Render the FLUID contract to a Bitol bundle so we have physical
    S1/S2/S3/S4 shapes on disk."""
    with open(fluid_path) as f:
        fluid = yaml.safe_load(f)
    prov = BitolOdpsProvider()
    prov.strict_validation = False
    prov.render(fluid, out_dir=tmp_path)
    return tmp_path


# Single small fixture from the repo's own tests/fixtures/ — keeps the test
# hermetic. The biz-lab integration is exercised by the broader live E2E in
# the docker-up sandbox; this test pins the matrix shape against a known
# multi-port contract.
FLUID_FIXTURE = _FIXTURES / "fluid" / "contract-multi-expose.fluid.yaml"


SHAPES = [
    pytest.param("S1-odps-file", SHAPE_ODPS_FILE, id="S1-odps-file"),
    pytest.param("S2-odcs-file", SHAPE_ODCS_FILE, id="S2-odcs-file"),
    pytest.param("S3-dir-bundle", SHAPE_DIRECTORY, id="S3-dir-bundle"),
    pytest.param("S4-odcs-only", SHAPE_ODCS_ONLY_DIRECTORY, id="S4-odcs-only"),
]


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """Per-test bundle so writes don't bleed across cases."""
    return _render_bundle(tmp_path, FLUID_FIXTURE)


def _input_for_shape(bundle_dir: Path, shape_label: str, tmp_path: Path) -> Path:
    """Materialise the right input path for each shape, derived from the
    same bundle so all four shapes describe the same underlying contract.
    """
    if shape_label == "S1-odps-file":
        return next(bundle_dir.glob("*.odps.yaml"))
    if shape_label == "S2-odcs-file":
        return sorted(bundle_dir.glob("*.odcs.yaml"))[0]
    if shape_label == "S3-dir-bundle":
        return bundle_dir
    if shape_label == "S4-odcs-only":
        odcs_only = tmp_path / "odcs-only"
        odcs_only.mkdir(exist_ok=True)
        for odcs_file in bundle_dir.glob("*.odcs.yaml"):
            shutil.copy(odcs_file, odcs_only)
        return odcs_only
    raise ValueError(shape_label)


def _import_fluid(input_path: Path) -> dict:
    """OP1: what ``fluid opds import`` runs to produce a final FLUID."""
    prov = BitolOdpsProvider()
    prov.strict_validation = False
    if input_path.is_dir():
        return prov.import_directory(input_path)
    if input_path.name.endswith(".odcs.yaml"):
        return OdcsProvider().import_contract(input_path)
    return prov.import_contract(input_path)


def _seed_fluid(input_path: Path) -> dict:
    """OP2: what ``fluid forge --seed-from`` runs (the pre-processor)."""
    return load_seed(input_path, allow_remote=False).fluid


def _counts(fluid: dict) -> Tuple[int, int]:
    """Two numbers we care about across both paths: number of exposes and
    total schema fields summed across exposes."""
    n_exposes = len(fluid.get("exposes") or [])
    n_fields = sum(len(e.get("contract", {}).get("schema", [])) for e in fluid.get("exposes") or [])
    return n_exposes, n_fields


# ---------------------------------------------------------------------------
# OP1 — opds import: every shape produces a valid FLUID
# ---------------------------------------------------------------------------


class TestImportEveryShape:
    @pytest.mark.parametrize("shape_label,_", SHAPES)
    def test_import_yields_well_formed_fluid(
        self, bundle: Path, tmp_path: Path, shape_label: str, _: str
    ) -> None:
        input_path = _input_for_shape(bundle, shape_label, tmp_path)
        fluid = _import_fluid(input_path)
        assert "exposes" in fluid
        assert isinstance(fluid["exposes"], list)
        # Every shape must produce at least one expose with a populated schema
        n_exposes, n_fields = _counts(fluid)
        assert n_exposes >= 1, f"{shape_label}: no exposes produced"
        assert n_fields >= 1, f"{shape_label}: no schema fields imported"


# ---------------------------------------------------------------------------
# OP2 — forge --seed-from: every shape produces a seed with the right shape
# label, provenance, and a working diff guard
# ---------------------------------------------------------------------------


class TestSeedEveryShape:
    @pytest.mark.parametrize("shape_label,expected_shape", SHAPES)
    def test_seed_shape_detected(
        self,
        bundle: Path,
        tmp_path: Path,
        shape_label: str,
        expected_shape: str,
    ) -> None:
        input_path = _input_for_shape(bundle, shape_label, tmp_path)
        seed = load_seed(input_path, allow_remote=False)
        assert seed.shape == expected_shape

    @pytest.mark.parametrize("shape_label,_", SHAPES)
    def test_seed_provenance_non_empty(
        self, bundle: Path, tmp_path: Path, shape_label: str, _: str
    ) -> None:
        input_path = _input_for_shape(bundle, shape_label, tmp_path)
        seed = load_seed(input_path, allow_remote=False)
        # Every successful seed has at least one provenance entry
        assert len(seed.provenance) >= 1

    @pytest.mark.parametrize("shape_label,_", SHAPES)
    def test_seed_ground_truth_paths_match_exposes(
        self, bundle: Path, tmp_path: Path, shape_label: str, _: str
    ) -> None:
        """``ground_truth_paths`` covers every expose's
        ``contract.schema``."""
        input_path = _input_for_shape(bundle, shape_label, tmp_path)
        seed = load_seed(input_path, allow_remote=False)
        n_exposes = len(seed.fluid.get("exposes", []))
        schema_paths = [p for p in seed.ground_truth_paths if p.endswith(".contract.schema")]
        assert len(schema_paths) == n_exposes

    @pytest.mark.parametrize("shape_label,_", SHAPES)
    def test_seed_diff_guard_catches_schema_mutation(
        self, bundle: Path, tmp_path: Path, shape_label: str, _: str
    ) -> None:
        """Wiping the first expose's schema must trip the ground-truth diff."""
        input_path = _input_for_shape(bundle, shape_label, tmp_path)
        seed = load_seed(input_path, allow_remote=False)
        if not seed.fluid.get("exposes"):
            pytest.skip("no exposes to mutate")
        mutated = yaml.safe_load(yaml.dump(seed.fluid))
        mutated["exposes"][0].setdefault("contract", {})["schema"] = []
        mismatches = diff_against_seed(seed, mutated)
        assert mismatches, f"{shape_label}: diff guard failed to catch schema mutation"

    @pytest.mark.parametrize("shape_label,_", SHAPES)
    def test_seed_diff_guard_identity_is_clean(
        self, bundle: Path, tmp_path: Path, shape_label: str, _: str
    ) -> None:
        """Feeding the seed itself back through the diff must produce zero
        mismatches."""
        input_path = _input_for_shape(bundle, shape_label, tmp_path)
        seed = load_seed(input_path, allow_remote=False)
        assert diff_against_seed(seed, seed.fluid) == []


# ---------------------------------------------------------------------------
# Cross-check: import and seed paths agree on (#exposes, #fields)
# ---------------------------------------------------------------------------


class TestImportAndSeedAgree:
    """The two paths share their underlying mappers — drift between them
    would mean the LLM gets a different view of the contract than ``opds
    import`` produces. These tests pin them together.
    """

    @pytest.mark.parametrize("shape_label,_", SHAPES)
    def test_counts_match(self, bundle: Path, tmp_path: Path, shape_label: str, _: str) -> None:
        input_path = _input_for_shape(bundle, shape_label, tmp_path)
        op1 = _counts(_import_fluid(input_path))
        op2 = _counts(_seed_fluid(input_path))
        assert op1 == op2, (
            f"{shape_label}: import vs seed disagree. "
            f"import={op1[0]}exposes/{op1[1]}fields  "
            f"seed={op2[0]}exposes/{op2[1]}fields"
        )

    @pytest.mark.parametrize("shape_label,_", SHAPES)
    def test_expose_ids_match(self, bundle: Path, tmp_path: Path, shape_label: str, _: str) -> None:
        """Same expose ids in same order — a stronger pin than just counts."""
        input_path = _input_for_shape(bundle, shape_label, tmp_path)
        op1_ids = [e.get("id") for e in _import_fluid(input_path).get("exposes", [])]
        op2_ids = [e.get("id") for e in _seed_fluid(input_path).get("exposes", [])]
        assert op1_ids == op2_ids
