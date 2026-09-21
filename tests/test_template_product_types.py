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

"""The shipped quickstarts carry their Data Mesh classification.

`fluid init --template <name>` copies these contracts verbatim into the
user's workspace, so whatever `metadata.productType` they declare is what
a new user starts from -- and what the marketplace facets and the cloud
tag emitters (`fluid_product_type`) propagate downstream.

The classification is a reviewed decision per template, not something a
future edit should be able to change silently, so the mapping is frozen
here. `hello-world` is deliberately absent: it has no upstreams and no
consumers, exists only to prove the toolchain runs, and classifying it
would assert a mesh role it does not play.
"""

from __future__ import annotations

import pathlib

import pytest

import fluid_build
from fluid_build.forge.product_types import (
    ProductTypeError,
    normalize_metadata_in_place,
)
from fluid_build.util.safe_yaml import load_yaml_safe

TEMPLATES_DIR = pathlib.Path(fluid_build.__file__).parent / "templates"

#: The reviewed classification. SDP = source-aligned (lands raw data),
#: ADP = aggregate (transforms one or more upstreams), CDP = consumer-aligned
#: (serves a named consumer). Changing an entry here is a product decision.
EXPECTED_PRODUCT_TYPES = {
    "contract-documentation": "SDP",
    "csv-basics": "SDP",
    "customer-360": "CDP",
    "data-quality-validation": "SDP",
    "environment-configuration": "SDP",
    "external-sql-files": "ADP",
    "first-dag": "ADP",
    "incremental-processing": "ADP",
    "multi-source": "ADP",
    "multiple-outputs": "CDP",
    "pipeline-orchestration": "ADP",
    "testing-your-contract": "SDP",
}

#: Templates that intentionally carry no productType, with the reason.
DELIBERATELY_UNCLASSIFIED = {
    "hello-world": "toolchain smoke test -- no upstreams, no consumers, no mesh role",
}


def _shipped_contracts():
    """Every `contract.fluid.yaml` under the shipped templates directory."""
    return sorted(TEMPLATES_DIR.glob("*/contract.fluid.yaml"))


def _metadata(path: pathlib.Path) -> dict:
    contract = load_yaml_safe(path.read_text(encoding="utf-8")) or {}
    metadata = contract.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def test_every_shipped_template_is_accounted_for():
    """Coverage guard: a green run must mean the files were actually read.

    Without this, deleting the templates directory -- or a glob that stops
    matching -- turns every other assertion below into a vacuous pass over
    an empty list.
    """
    found = {p.parent.name for p in _shipped_contracts()}
    assert found, f"no shipped template contracts found under {TEMPLATES_DIR}"

    expected = set(EXPECTED_PRODUCT_TYPES) | set(DELIBERATELY_UNCLASSIFIED)
    assert found == expected, (
        "the shipped templates and this test's mapping have diverged; "
        f"only on disk: {sorted(found - expected)}; "
        f"only in the mapping: {sorted(expected - found)}"
    )


@pytest.mark.parametrize("name,product_type", sorted(EXPECTED_PRODUCT_TYPES.items()))
def test_template_declares_its_reviewed_product_type(name, product_type):
    metadata = _metadata(TEMPLATES_DIR / name / "contract.fluid.yaml")
    assert metadata.get("productType") == product_type, (
        f"template {name!r} declares productType="
        f"{metadata.get('productType')!r}, expected {product_type!r}"
    )


@pytest.mark.parametrize("name,reason", sorted(DELIBERATELY_UNCLASSIFIED.items()))
def test_unclassified_template_stays_unclassified(name, reason):
    metadata = _metadata(TEMPLATES_DIR / name / "contract.fluid.yaml")
    assert "productType" not in metadata, (
        f"template {name!r} is deliberately unclassified ({reason}) but now "
        f"declares productType={metadata.get('productType')!r}"
    )


@pytest.mark.parametrize("path", _shipped_contracts(), ids=lambda p: p.parent.name)
def test_shipped_metadata_survives_the_canonical_normalizer(path):
    """A layer/productType pair that disagrees ships a contract `fluid validate` rejects.

    `normalize_metadata_in_place` is the single cross-check used by
    `cli/validate.py` and `cli/contract.py`; running it here catches an
    inconsistent pair (Gold + SDP, say) at the template rather than in the
    user's first `fluid validate`.
    """
    metadata = dict(_metadata(path))
    try:
        normalize_metadata_in_place(metadata)
    except ProductTypeError as exc:  # pragma: no cover - the failure message is the point
        pytest.fail(f"{path.parent.name}: {exc}")
