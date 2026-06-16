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

"""Regression tests for the dbt `select` / `models` follow-up to issue #249.

Same bug class as #249: the contract schema rejected fields the dbt build
runner already honours. ``build_dbt_command`` reads
``build.properties.select`` / ``build.properties.models`` via
``_normalize_selectors`` (``fluid_build/build_runners/dbt/runner.py``) and
forwards them to ``dbt --select`` — but ``hybridReferencePattern``
(``additionalProperties: false``) listed neither, so a build using them failed
``fluid validate`` with "Additional properties are not allowed ('select' ...)".

``_normalize_selectors`` accepts a single string OR a list of strings, so the
schema models both shapes (``oneOf: [string, array<string>]``) in all four
bundled schemas (0.7.1–0.7.4). These tests pin both shapes as valid and a
wrong-typed selector as invalid, and confirm ``additionalProperties: false`` is
still in force.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fluid_build.schema_manager import FluidSchemaManager

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "contracts" / "compatibility"

BUNDLED_VERSIONS = list(FluidSchemaManager.BUNDLED_VERSIONS)
LATEST = FluidSchemaManager.latest_bundled_version()


def _minimal_contract(version: str) -> dict:
    slug = version.replace(".", "")
    fixture = FIXTURE_DIR / f"minimal_{slug}.yaml"
    with fixture.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _with_dbt_build(version: str, props: dict) -> dict:
    contract = _minimal_contract(version)
    contract["builds"] = [
        {
            "id": "transform",
            "pattern": "hybrid-reference",
            "engine": "dbt",
            "properties": {"model": "subscriber_360", **props},
        }
    ]
    return contract


def _validate(contract: dict, version: str):
    return FluidSchemaManager().validate_contract(
        contract, schema_version=version, offline_only=True
    )


def _messages(result) -> list[str]:
    return [getattr(e, "message", str(e)) for e in result.errors]


@pytest.mark.parametrize("version", BUNDLED_VERSIONS, ids=[f"v{v}" for v in BUNDLED_VERSIONS])
@pytest.mark.parametrize(
    "props",
    [
        {"select": "subscriber_360+"},
        {"select": ["subscriber_360", "tag:nightly"]},
        {"models": "subscriber_360"},
        {"models": ["a", "b"]},
        {"select": ["+subscriber_360"], "models": "legacy_alias"},
    ],
    ids=["select-str", "select-list", "models-str", "models-list", "both"],
)
def test_select_models_accepted(version: str, props: dict):
    """A hybrid-reference dbt build with select/models (string or array)
    validates on every bundled schema."""
    result = _validate(_with_dbt_build(version, props), version)
    assert result.is_valid, f"{version} props={props}: {_messages(result)}"


@pytest.mark.parametrize(
    "props",
    [
        {"select": 123},
        {"select": [1, 2, 3]},
        {"models": {"not": "a-selector"}},
    ],
    ids=["select-int", "select-int-list", "models-object"],
)
def test_wrong_typed_selector_rejected(props: dict):
    """The oneOf[string, array<string>] shape rejects non-string selectors."""
    result = _validate(_with_dbt_build(LATEST, props), LATEST)
    assert not result.is_valid, f"props={props} unexpectedly accepted"


def test_unknown_property_still_rejected():
    """additionalProperties:false is preserved — a typo'd selector key fails."""
    result = _validate(_with_dbt_build(LATEST, {"selct": "typo"}), LATEST)
    assert not result.is_valid
    assert "selct" in " ".join(_messages(result))
