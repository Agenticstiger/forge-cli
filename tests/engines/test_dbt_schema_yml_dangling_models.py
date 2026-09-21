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

"""A schema.yml entry for a model nobody emitted must not ship.

`generate_schema_yml` names entries after the *expose*, while the intent,
multi-stage and embedded-logic emitters name their files from *stage*
names. On those paths every entry describes a model that does not exist.

dbt does not fail on that. It warns ``NoNodeForYamlKey (dbt1089)`` and
carries on -- so the column descriptions, the ``access: public`` configs
and, worst of all, the **data tests** the contract declared attach to
nothing and silently never run. The project reports as passing.

Found by running a real dbt v2 engine over the shipped ``customer-360``
template, where it cost 9 tests: ``unique``, ``not_null`` and an email
validation, none of which had executed since the template shipped.

Same defect and same remedy as ``generate_semantic_models`` (#621) --
which dbt *does* reject outright, which is why that half was noticed and
this half was not.
"""

from __future__ import annotations

import logging

import pytest
import yaml

from fluid_build.engines.dbt.schema_yml import generate_schema_yml

pytestmark = pytest.mark.unit


def _contract():
    """Two exposes carrying tests, as customer-360 does."""
    return {
        "fluidVersion": "0.7.5",
        "id": "analytics.customer_360",
        "exposes": [
            {
                "exposeId": "customer_360_master",
                "kind": "table",
                "contract": {
                    "schema": [
                        {"name": "customer_id", "type": "string", "required": True, "unique": True},
                        {"name": "email", "type": "string"},
                    ]
                },
            },
            {
                "exposeId": "high_value_customers",
                "kind": "table",
                "contract": {"schema": [{"name": "customer_id", "type": "string", "required": True}]},
            },
        ],
    }


def _models(files):
    """The model entries in the emitted schema.yml, whatever its path."""
    for path, content in files.items():
        if path.endswith("schema.yml"):
            return yaml.safe_load(content).get("models", [])
    return []


class TestDanglingSchemaEntriesAreDropped:
    def test_entries_without_an_emitted_model_are_dropped(self, caplog):
        """The stage-named case: no expose has a model, so nothing ships."""
        stage_named = {"stg_customers", "fct_customer_360"}

        with caplog.at_level(logging.WARNING, logger="fluid_build.engines.dbt.schema_yml"):
            files = generate_schema_yml(_contract(), emitted_models=stage_named)

        assert _models(files) == [], (
            "entries naming models that were never emitted must not ship -- "
            "dbt only warns on them, so their tests silently never run"
        )
        blob = " ".join(r.getMessage() for r in caplog.records)
        assert "dbt_schema_yml_model_missing" in blob
        # Naming both the missing model and what WAS emitted is the point:
        # the reader needs to see the mismatch to resolve it.
        assert "customer_360_master" in blob
        assert "high_value_customers" in blob
        assert "stg_customers" in blob

    def test_entries_with_an_emitted_model_are_kept(self):
        """The expose-named case (the untyped skeleton path) is untouched."""
        files = generate_schema_yml(
            _contract(), emitted_models={"customer_360_master", "high_value_customers"}
        )
        names = {m["name"] for m in _models(files)}
        assert names == {"customer_360_master", "high_value_customers"}

    def test_a_partial_match_keeps_only_what_exists(self):
        files = generate_schema_yml(_contract(), emitted_models={"customer_360_master"})
        assert {m["name"] for m in _models(files)} == {"customer_360_master"}

    def test_omitting_the_set_keeps_the_old_behaviour(self):
        """Additive: callers that pass nothing are unchanged.

        Several callers construct schema.yml without knowing the emitted
        set; silently dropping everything for them would be a far worse
        regression than the warning this fixes.
        """
        files = generate_schema_yml(_contract())
        assert {m["name"] for m in _models(files)} == {
            "customer_360_master",
            "high_value_customers",
        }

    def test_the_dropped_entry_really_did_carry_tests(self):
        """Pins WHY this matters rather than just that entries vanish.

        If the emitted schema.yml carried no tests, dropping the entries
        would be cosmetic. It is not: these are the contract's declared
        data-quality checks, and they were silently inert.
        """
        files = generate_schema_yml(_contract())
        tests = [
            t
            for m in _models(files)
            for c in m.get("columns", [])
            for k in ("tests", "data_tests")
            for t in c.get(k, [])
        ]
        assert tests, "expected the contract's declared column tests in schema.yml"


class TestTheEngineActuallyPassesTheEmittedSet:
    """The helper working proves nothing about whether anything calls it.

    Every test above calls ``generate_schema_yml`` directly. Removing
    ``emitted_models=`` from the one call site in
    ``engines/dbt/__init__.py`` left them all green while the defect came
    straight back -- verified by mutation. This drives the real engine.
    """

    def test_generated_project_has_no_dangling_schema_entries(self, tmp_path):
        from fluid_build.engines.dbt import DbtEngine

        contract = {
            "fluidVersion": "0.7.5",
            "id": "analytics.customer_360",
            "name": "Customer 360",
            "exposes": [
                {
                    "exposeId": "customer_360_master",
                    "kind": "table",
                    "binding": {"platform": "local", "format": "parquet"},
                    "contract": {
                        "schema": [
                            {
                                "name": "customer_id",
                                "type": "string",
                                "required": True,
                                "unique": True,
                            }
                        ]
                    },
                }
            ],
            "builds": [
                {
                    "id": "b1",
                    "engine": "dbt",
                    "pattern": "multi-stage",
                    "properties": {
                        "stages": [
                            {
                                "name": "stg_customers",
                                "outputs": ["stg_customers"],
                                "properties": {"sql": "SELECT 1 AS customer_id"},
                            },
                            {
                                "name": "fct_customer_360",
                                "dependsOn": ["stg_customers"],
                                "outputs": ["fct_customer_360"],
                                "properties": {"sql": "SELECT * FROM stg_customers"},
                            },
                        ]
                    },
                }
            ],
        }

        files = DbtEngine().generate(contract, contract["builds"][0], output_dir=tmp_path)

        emitted = {
            path.rsplit("/", 1)[-1][: -len(".sql")]
            for path in files
            if path.startswith("models/") and path.endswith(".sql")
        }
        declared = {m["name"] for m in _models(files)}
        dangling = declared - emitted
        assert not dangling, (
            f"schema.yml declares models that were never emitted: {sorted(dangling)}. "
            f"Emitted: {sorted(emitted)}. dbt only warns (dbt1089), so their tests "
            "would silently never run."
        )
