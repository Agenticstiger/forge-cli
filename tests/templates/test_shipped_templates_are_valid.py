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

"""Every template `fluid init` can scaffold must be a contract that validates.

Written because `multiple-outputs` was NOT. It declared
`metadata.layer: 'Multi-Layer'`, which is not in the permitted set, so a reader
who scaffolded it got a contract that `fluid validate` rejected immediately --
and nothing in the suite noticed, through every release.

The existing template tests check that scaffolding PRODUCES a
`contract.fluid.yaml` and that the file exists. None of them ever validated its
CONTENT.

**Why this exercises two checks and not one.** `validate_contract_file` returns
`is_valid=True` for the broken template: JSON-schema validation passes, because
`metadata.layer` is a free-form string as far as the schema is concerned. The
refusal comes from a SECOND layer -- `normalize_metadata_in_place`, which
`cli/validate.py` calls separately and reports as "metadata consistency".

So a fence built on the obvious API would have passed on the one file we already
know is broken. That is the whole trap this repo keeps falling into, and it
nearly caught this test too. Both layers are asserted, in the same order the CLI
runs them.
"""

from __future__ import annotations

import copy
import pathlib

import pytest
import yaml

from fluid_build.forge.product_types import ProductTypeError, normalize_metadata_in_place
from fluid_build.schema_manager import validate_contract_file

TEMPLATES = pathlib.Path(__file__).resolve().parents[2] / "fluid_build" / "templates"
CONTRACTS = sorted(TEMPLATES.glob("*/contract.fluid.yaml"))


def test_there_are_templates_to_check():
    """A glob that matches nothing would make every test below vacuously green."""
    assert len(CONTRACTS) >= 10, f"only found {len(CONTRACTS)} template contracts under {TEMPLATES}"


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda p: p.parent.name)
def test_template_passes_schema_validation(contract: pathlib.Path):
    result = validate_contract_file(str(contract))
    assert result.is_valid, (
        f"{contract.parent.name} fails schema validation: " f"{getattr(result, 'errors', None)}"
    )


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda p: p.parent.name)
def test_template_passes_metadata_consistency(contract: pathlib.Path):
    """The check `cli/validate.py` runs after the schema, and reports separately.

    This is the one that catches a bad `metadata.layer` / `metadata.productType`.
    """
    doc = yaml.safe_load(contract.read_text(encoding="utf-8"))
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        pytest.skip(f"{contract.parent.name} declares no metadata block")
    try:
        normalize_metadata_in_place(copy.deepcopy(metadata))
    except ProductTypeError as exc:
        pytest.fail(f"{contract.parent.name} fails metadata consistency: {exc}")


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda p: p.parent.name)
def test_template_declares_a_bundled_schema_version(contract: pathlib.Path):
    """A template pinned to a version this CLI does not bundle cannot validate."""
    from fluid_build.schema_manager import FluidSchemaManager

    doc = yaml.safe_load(contract.read_text(encoding="utf-8"))
    declared = str(doc.get("fluidVersion", "")).strip()
    assert declared, f"{contract.parent.name} declares no fluidVersion"
    bundled = set(FluidSchemaManager.BUNDLED_VERSIONS)
    assert declared in bundled, (
        f"{contract.parent.name} pins fluidVersion {declared!r}, which this CLI "
        f"does not bundle (has {sorted(bundled)})"
    )
    # A template must not ship pinned to a PREVIEW schema: a scaffold is the
    # first contract a new user owns, and it should not be on a version the
    # product does not call stable.
    assert declared not in FluidSchemaManager.PREVIEW_VERSIONS, (
        f"{contract.parent.name} pins the preview schema {declared!r}; "
        f"scaffolds should pin a stable version"
    )
