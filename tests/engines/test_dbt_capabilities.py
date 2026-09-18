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

"""The dbt capability floors, pinned to what was actually measured.

Each expectation here corresponds to a real `dbt parse` / `dbt source
freshness` run against that exact dbt version — see the module docstring of
`fluid_build/engines/dbt/_capabilities.py` for the matrix. They are pinned as
tests because two of the three shapes fail SILENTLY on older dbt: the project
still parses, the contract's freshness promise just stops being honoured.
"""

from __future__ import annotations

import pytest

from fluid_build.engines.dbt._capabilities import (
    FRESHNESS_CONFIG_FLOOR,
    TEST_ARGUMENTS_FLOOR,
    resolve_dbt_capabilities,
)


@pytest.mark.parametrize(
    "version,arguments,freshness_config",
    [
        # Measured: arguments -> compile error; freshness config -> source skipped.
        ("1.8.10", False, False),
        # Measured: "loaded_at_field must be specified" -> config silently ignored.
        ("1.9.11", False, False),
        ("1.10.0", False, False),
        # Measured: freshness config honoured here; arguments still gated behind
        # require_generic_test_arguments_property, which defaults false.
        ("1.10.5", False, True),
        ("1.10.7", False, True),
        # Measured: the behaviour flag defaults true from here.
        ("1.10.8", True, True),
        ("1.12.5", True, True),
    ],
)
def test_core_floors_match_what_was_measured(version, arguments, freshness_config):
    caps = resolve_dbt_capabilities("core", version)
    assert caps.nest_test_arguments is arguments
    assert caps.config_scoped_source_freshness is freshness_config


def test_v2_gets_every_modern_shape():
    """dbt v2 rejects all three legacy shapes outright, so none may be emitted."""
    for version in ("2.0.4", "2.0.1", "2.0.0-preview.126"):
        caps = resolve_dbt_capabilities("fusion", version)
        assert caps.nest_test_arguments is True
        assert caps.config_scoped_source_freshness is True


@pytest.mark.parametrize("flavor,version", [("unknown", ""), (None, ""), ("core", "")])
def test_undetectable_engine_falls_back_to_the_legacy_shapes(flavor, version):
    """Legacy parses on every dbt 1.x; a v2 user has a v2 binary to detect."""
    caps = resolve_dbt_capabilities(flavor, version)
    assert caps.nest_test_arguments is False
    assert caps.config_scoped_source_freshness is False


def test_unparseable_version_does_not_crash_or_guess_high():
    for junk in ("latest", "v", "not-a-version", "..", "1.x"):
        caps = resolve_dbt_capabilities("core", junk)
        assert caps.nest_test_arguments is False


def test_prerelease_suffixes_compare_on_the_numeric_part():
    assert resolve_dbt_capabilities("core", "1.10.8b1").nest_test_arguments is True
    assert resolve_dbt_capabilities("core", "1.10.7rc2").nest_test_arguments is False


def test_floors_are_the_measured_values():
    """Guard against someone 'tidying' these to the documented numbers.

    dbt's docs give the arguments floor as 1.10.5; measurement says the
    behaviour flag only defaults true at 1.10.8, and 1.10.5/1.10.7 really do
    fail. Do not relax these without re-running the matrix.
    """
    assert TEST_ARGUMENTS_FLOOR == (1, 10, 8)
    assert FRESHNESS_CONFIG_FLOOR == (1, 10, 5)


# ---------------------------------------------------------------------------
# The `arguments:` transform itself.
# ---------------------------------------------------------------------------


def test_nesting_moves_inputs_under_arguments():
    from fluid_build.engines.dbt._test_mapping import nest_test_arguments as nest

    assert nest({"accepted_values": {"values": [1, 2]}}) == {
        "accepted_values": {"arguments": {"values": [1, 2]}}
    }
    assert nest({"relationships": {"to": "ref('x')", "field": "id"}}) == {
        "relationships": {"arguments": {"to": "ref('x')", "field": "id"}}
    }


def test_nesting_leaves_reserved_keys_beside_arguments():
    """`config`/`description` are dbt's, not test inputs.

    Per the v2 spec CustomTestInner is additionalProperties:false over
    {arguments, column_name, config, description, name} -- moving `config`
    under `arguments` would make the document invalid.
    """
    from fluid_build.engines.dbt._test_mapping import nest_test_arguments as nest

    assert nest({"t": {"config": {"severity": "warn"}, "threshold": 5}}) == {
        "t": {"config": {"severity": "warn"}, "arguments": {"threshold": 5}}
    }


def test_nesting_is_idempotent_and_ignores_argumentless_tests():
    from fluid_build.engines.dbt._test_mapping import nest_test_arguments as nest

    already = {"accepted_values": {"arguments": {"values": [1]}}}
    assert nest(already) == already
    assert nest("not_null") == "not_null"  # bare string test
    assert nest({"unique": {}}) == {"unique": {}}  # no inputs to move


def test_emitted_schema_yml_switches_shape_at_the_floor():
    """End-to-end: the emitter honours the capability, both ways.

    Measured against real engines: the nested form is a compile error below
    dbt-core 1.10.8, and the flat form is a hard error (dbt1159) on v2 -- so
    emitting either unconditionally breaks someone.
    """
    import yaml

    from fluid_build.engines.dbt._capabilities import LEGACY, MODERN
    from fluid_build.engines.dbt.schema_yml import generate_schema_yml

    contract = {
        "fluidVersion": "0.7.5",
        "id": "g.a.orders_v1",
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "contract": {
                    "schema": [{"name": "status", "type": "STRING"}],
                    "dq": {
                        "rules": [
                            {
                                "id": "r1",
                                "type": "uniqueness",
                                "severity": "error",
                                "selector": "status",
                            }
                        ]
                    },
                },
            }
        ],
    }
    legacy = yaml.safe_load(
        generate_schema_yml(contract, capabilities=LEGACY)["models/marts/schema.yml"]
    )
    modern = yaml.safe_load(
        generate_schema_yml(contract, capabilities=MODERN)["models/marts/schema.yml"]
    )
    assert "arguments" not in yaml.safe_dump(legacy)
    # access moves under config on BOTH -- that one has no floor.
    for parsed in (legacy, modern):
        assert parsed["models"][0]["config"]["access"] == "public"


def test_orphan_dq_column_tests_are_nested_too():
    """A dq rule on a column absent from contract.schema[] lands on a separate
    emit path, which originally skipped the nesting -- so one generated file
    carried both shapes, and the flat one aborts a v2 parse of the whole
    project.
    """
    import yaml

    from fluid_build.engines.dbt._capabilities import MODERN
    from fluid_build.engines.dbt.schema_yml import generate_schema_yml

    contract = {
        "fluidVersion": "0.7.5",
        "id": "g.a.orders_v1",
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "contract": {
                    "schema": [{"name": "status", "type": "STRING"}],
                    "dq": {
                        "rules": [
                            # declared column -> the already-covered path
                            {
                                "id": "r1",
                                "type": "uniqueness",
                                "severity": "error",
                                "selector": "status",
                            },
                            # NOT in schema[] -> the orphan-column path
                            {
                                "id": "r2",
                                "type": "accuracy",
                                "severity": "error",
                                "selector": "not_in_schema",
                                "operator": ">=",
                                "threshold": 0.9,
                            },
                        ]
                    },
                },
            }
        ],
    }
    emitted = generate_schema_yml(contract, capabilities=MODERN)["models/marts/schema.yml"]
    parsed = yaml.safe_load(emitted)
    for column in parsed["models"][0].get("columns", []):
        for test in column.get("data_tests", []) or column.get("tests", []) or []:
            if isinstance(test, dict):
                ((_name, body),) = test.items()
                if isinstance(body, dict) and body:
                    assert (
                        "arguments" in body
                    ), f"column {column['name']} emitted a flat test: {test}"
