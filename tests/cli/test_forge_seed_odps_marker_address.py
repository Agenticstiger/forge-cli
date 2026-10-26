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

"""``fluid forge --seed-from <dir>`` must still recognise an ODCS-only directory.

The ODPS importer marks a wrapper-less directory — one holding raw ODCS
contracts and no ``kind: DataProduct`` document — with an
``odcs_only_directory`` flag, and the seed pre-processor reads that flag to
decide between the ``directory`` and ``odcs-only-directory`` shapes. The shape
is what tells the copilot whether its seed came with a product wrapper or
whether it has to invent one.

The flag moved. Its inline home was ``metadata.odps_passthrough``, which the
FLUID schema's closed ``metadata`` object could never hold, so the importer now
hoists the whole bucket into root ``extensions.odps`` before returning. The
seed loader went on reading the pre-hoist address, found ``{}`` every time, and
reported every ODCS-only directory as a plain bundle — no error, no warning,
just the wrong shape handed to the LLM.

So these tests drive the real importer and the real loader against a real
wrapper-less directory on disk. Two things are pinned, deliberately at two
different levels: the shape the loader detects (the behaviour users get) and
the address the marker actually occupies (the coupling that broke). Relocate
the bucket again without updating the reader and the address test says exactly
what moved, instead of leaving a shape assertion to fail for an unexplained
reason.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.cli.forge_copilot_seed import (
    SHAPE_DIRECTORY,
    SHAPE_ODCS_ONLY_DIRECTORY,
    load_seed,
)
from fluid_build.providers.odps_standard import BitolOdpsProvider

# The fixture's synthetic input ports have no upstream contracts to resolve, so
# the resolver logs a "tried 8 paths" warning per port. Expected here, noise in
# the report.
logging.getLogger("fluid_build.providers.odps_standard.provider.BitolOdpsProvider").setLevel(
    logging.ERROR
)

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "fluid" / "contract-multi-expose.fluid.yaml"


@pytest.fixture
def odcs_only_dir(tmp_path: Path) -> Path:
    """A real wrapper-less directory: the ODCS halves of a rendered bundle.

    Rendered from a FLUID fixture rather than hand-written, so the ODCS
    documents are exactly what this codebase emits and the test cannot pass
    against a shape the exporter no longer produces.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    provider = BitolOdpsProvider()
    provider.strict_validation = False
    with open(_FIXTURE) as handle:
        provider.render(yaml.safe_load(handle), out_dir=bundle)

    odcs_only = tmp_path / "odcs-only"
    odcs_only.mkdir()
    for odcs_file in bundle.glob("*.odcs.yaml"):
        shutil.copy(odcs_file, odcs_only)
    # No product wrapper — that is the whole point of the shape.
    assert not list(odcs_only.glob("*.odps.yaml"))
    assert list(odcs_only.glob("*.odcs.yaml")), "render produced no ODCS contracts"
    return odcs_only


def _import(odcs_only_dir: Path) -> Dict[str, Any]:
    provider = BitolOdpsProvider()
    provider.strict_validation = False
    return provider.import_directory(odcs_only_dir, allow_remote=False, lenient=True)


class TestOdcsOnlyDirectoryShape:
    def test_seed_detects_odcs_only_not_plain_directory(self, odcs_only_dir: Path) -> None:
        """The behaviour the regression stole: the shape the loader reports."""
        seed = load_seed(odcs_only_dir, allow_remote=False)
        assert seed.shape == SHAPE_ODCS_ONLY_DIRECTORY, (
            f"a directory with no ODPS wrapper was reported as {seed.shape!r}; "
            "the loader is reading the ODCS-only marker at the wrong address"
        )
        assert seed.shape != SHAPE_DIRECTORY

    def test_provenance_carries_the_detected_shape(self, odcs_only_dir: Path) -> None:
        """Provenance repeats the shape, so a wrong shape also corrupts the
        audit trail downstream consumers read."""
        seed = load_seed(odcs_only_dir, allow_remote=False)
        assert seed.provenance[0]["shape"] == SHAPE_ODCS_ONLY_DIRECTORY

    def test_a_real_bundle_is_still_a_plain_directory(self, tmp_path: Path) -> None:
        """The other half of the branch: a directory that does have a product
        wrapper must not be mistaken for ODCS-only."""
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        provider = BitolOdpsProvider()
        provider.strict_validation = False
        with open(_FIXTURE) as handle:
            provider.render(yaml.safe_load(handle), out_dir=bundle)

        seed = load_seed(bundle, allow_remote=False)
        assert seed.shape == SHAPE_DIRECTORY


class TestOdcsOnlyMarkerAddress:
    """Pin the address itself, so the next relocation is caught at the source.

    The shape test above fails when the reader and the writer disagree, but it
    cannot say which side moved. These assertions name the address, so a future
    relocation of the pass-through bucket fails with the move spelled out.
    """

    def test_marker_lives_under_extensions_odps_metadata(self, odcs_only_dir: Path) -> None:
        fluid = _import(odcs_only_dir)
        bucket = ((fluid.get("extensions") or {}).get("odps") or {}).get("metadata") or {}
        assert bucket.get("odcs_only_directory") is True, (
            "the ODCS-only marker is no longer at extensions.odps.metadata."
            "odcs_only_directory; _load_directory in "
            "fluid_build/cli/forge_copilot_seed.py reads that address and must "
            "move with it"
        )
        assert "odcs_only_warning" in bucket

    def test_marker_is_not_left_in_closed_metadata(self, odcs_only_dir: Path) -> None:
        """``metadata`` is ``additionalProperties: false`` in the FLUID schema,
        so a bucket left behind there is an invalid contract, not a second
        address a reader could fall back on."""
        fluid = _import(odcs_only_dir)
        assert "odps_passthrough" not in (fluid.get("metadata") or {})
