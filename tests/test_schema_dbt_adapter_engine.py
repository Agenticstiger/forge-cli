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

"""Regression tests for issue #249.

The contract schema must accept the two dbt build-runner features it
previously rejected:

* **Adapter-qualified engines** — ``dbt-<adapter>`` (``dbt-glue``,
  ``dbt-snowflake``, …). ``build_runners/base.py::is_dbt_build`` routes any
  ``dbt`` / ``dbt-*`` engine into the dbt path, and
  ``build_runners/dbt/runner.py::_infer_dbt_adapter`` derives the adapter
  from ``engine[4:]`` — so the schema must allow the whole ``dbt-*`` family
  generically, not a hand-maintained list.
* **``build.properties.target``** — ``build_dbt_command`` reads
  ``properties.target`` and forwards it as ``dbt --target <name>``.

The runner honours both regardless of contract version, so the relaxation
lands in every bundled schema (0.7.1–0.7.4). These tests pin that the
relaxation is generic (any ``dbt-<adapter>``) and that it did **not** widen
the schema into accepting garbage (bogus engines + unknown hybrid props
must still be rejected — ``additionalProperties: false`` is preserved).
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
    """Load the per-version minimal compatibility fixture (known-valid)."""
    slug = version.replace(".", "")
    fixture = FIXTURE_DIR / f"minimal_{slug}.yaml"
    with fixture.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _with_dbt_build(
    version: str,
    *,
    engine: str = "dbt-glue",
    target: str | None = "glue-interactive",
    extra_props: dict | None = None,
) -> dict:
    """Return a known-valid minimal contract whose single build is a
    hybrid-reference dbt build with the given engine / target."""
    contract = _minimal_contract(version)
    props: dict = {"model": "subscriber_360"}
    if target is not None:
        props["target"] = target
    if extra_props:
        props.update(extra_props)
    contract["builds"] = [
        {
            "id": "transform",
            "pattern": "hybrid-reference",
            "engine": engine,
            "properties": props,
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
def test_dbt_glue_engine_and_target_accepted(version: str):
    """``engine: dbt-glue`` + ``properties.target`` validates on every bundled
    schema (the exact repro from issue #249)."""
    result = _validate(_with_dbt_build(version), version)
    assert result.is_valid, f"{version}: {_messages(result)}"


@pytest.mark.parametrize(
    "engine",
    ["dbt", "dbt-snowflake", "dbt-bigquery", "dbt-redshift", "dbt-duckdb", "dbt-athena-community"],
)
def test_dbt_adapter_engines_accepted_generically(engine: str):
    """The relaxation is generic to the whole ``dbt-<adapter>`` family — not a
    hardcoded ``dbt-glue`` special case (mirrors the runner's ``engine[4:]``)."""
    result = _validate(_with_dbt_build(LATEST, engine=engine), LATEST)
    assert result.is_valid, f"engine={engine!r}: {_messages(result)}"


@pytest.mark.parametrize(
    "engine",
    ["frobnicate", "notdbt-foo", "dbt-", "dbtglue", "DBT-GLUE"],
)
def test_bogus_engine_still_rejected(engine: str):
    """Widening the enum must not accept arbitrary strings: a non-dbt engine,
    an empty adapter suffix, a missing hyphen, or wrong case all still fail."""
    result = _validate(_with_dbt_build(LATEST, engine=engine, target=None), LATEST)
    assert not result.is_valid, f"engine={engine!r} was unexpectedly accepted"


def test_unknown_hybrid_property_still_rejected():
    """``hybridReferencePattern.additionalProperties: false`` is preserved — a
    typo'd property is still caught (adding ``target`` did not open the gate)."""
    result = _validate(_with_dbt_build(LATEST, extra_props={"taget": "oops"}), LATEST)
    assert not result.is_valid
    assert "taget" in " ".join(_messages(result))
