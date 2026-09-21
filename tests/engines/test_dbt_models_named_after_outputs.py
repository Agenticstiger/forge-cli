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

"""A multi-stage model is named after its declared output, not its stage.

``stages[].outputs`` already carries the expose->stage mapping; the
emitter used to name files from ``stage.name`` and ignore it. That single
omission was the root cause of four separate symptoms:

* ``schema.yml`` declares models named after exposes, so dbt warns
  ``NoNodeForYamlKey (dbt1089)`` and the contract's declared data tests
  attach to nothing and never run,
* ``semantic_models.yml`` refs dangle the same way (deferred in #621),
* ``fluid verify --reconcile-dbt`` reports ``model_missing_in_dbt``,
* and the author's own stage SQL does not resolve -- the shipped
  ``customer-360`` contract writes ``FROM customer_base`` and
  ``FROM customer_360_master``, which are OUTPUT names, while the files
  were emitted as ``stage_1_customer_base.sql`` and
  ``stage_4_rfm_calculation.sql``.

Naming the model after the output fixes all four at once.
"""

from __future__ import annotations

import pytest

from fluid_build.engines.dbt.models import (
    _generate_multi_stage,
    _stage_model_names,
    generate_models,
)

pytestmark = pytest.mark.unit


def _contract(stages):
    return {
        "fluidVersion": "0.7.5",
        "id": "analytics.c360",
        "name": "C360",
        "exposes": [
            {
                "exposeId": "customer_360_master",
                "kind": "table",
                "binding": {"platform": "local", "format": "parquet"},
                "contract": {"schema": [{"name": "customer_id", "type": "string"}]},
            }
        ],
        "builds": [
            {
                "id": "b1",
                "engine": "dbt",
                "pattern": "multi-stage",
                "properties": {"stages": stages},
            }
        ],
    }


STAGES = [
    {
        "name": "stage_1_customer_base",
        "outputs": ["customer_base"],
        "properties": {"sql": "SELECT 1 AS customer_id"},
    },
    {
        "name": "stage_2_rfm",
        "dependsOn": ["stage_1_customer_base"],
        "outputs": ["customer_360_master"],
        "properties": {"sql": "SELECT * FROM customer_base"},
    },
]


def _model_names(files):
    return {p.rsplit("/", 1)[-1][: -len(".sql")] for p in files if p.endswith(".sql")}


class TestModelNaming:
    def test_models_take_their_declared_output_name(self):
        c = _contract(STAGES)
        files = generate_models(c, c["builds"][0])
        assert _model_names(files) == {"customer_base", "customer_360_master"}, (
            "models must be named after stages[].outputs, which is what the "
            "contract's exposes and the author's own SQL refer to"
        )

    def test_a_stage_without_outputs_keeps_its_own_name(self):
        """There is nothing better to use, and the skeleton paths rely on it."""
        stages = [{"name": "lonely_stage", "properties": {"sql": "SELECT 1"}}]
        c = _contract(stages)
        files = generate_models(c, c["builds"][0])
        assert _model_names(files) == {"lonely_stage"}

    def test_the_exposeid_now_has_a_model(self):
        """The whole point: the expose names a model that exists."""
        c = _contract(STAGES)
        files = generate_models(c, c["builds"][0])
        assert "customer_360_master" in _model_names(files)


class TestDependencyRefsFollowTheRename:
    def test_skeleton_refs_name_models_not_stages(self):
        """``dependsOn`` names STAGES; a dbt ``ref()`` must name a MODEL.

        Renaming the files without rewriting these would swap one dangling
        reference for another -- the emitted ``ref('stage_1_customer_base')``
        would point at a model that no longer exists.
        """
        stages = [
            {"name": "stage_1_base", "outputs": ["base_table"]},
            {"name": "stage_2_derived", "outputs": ["derived"], "dependsOn": ["stage_1_base"]},
        ]
        c = _contract(stages)
        files = generate_models(c, c["builds"][0])
        derived = next(v for k, v in files.items() if k.endswith("derived.sql"))
        assert "ref('base_table')" in derived, derived
        assert (
            "stage_1_base" not in derived
        ), "the ref still names the stage, which is not a model any more"


class TestTheMappingHelper:
    @pytest.mark.parametrize(
        "stage,expected",
        [
            ({"name": "s", "outputs": ["o"]}, "o"),
            ({"name": "s", "outputs": ["first", "second"]}, "first"),
            ({"name": "s", "outputs": []}, "s"),
            ({"name": "s"}, "s"),
            ({"name": "s", "outputs": [None, "later"]}, "later"),
            ({"name": "s", "outputs": ["", "later"]}, "later"),
        ],
        ids=["one", "first-wins", "empty-list", "absent", "skips-none", "skips-blank"],
    )
    def test_first_usable_output_wins(self, stage, expected):
        assert _stage_model_names([stage]) == {"s": expected}

    def test_malformed_stages_do_not_crash_generation(self):
        """This runs before every model is emitted; raising here would turn
        a naming improvement into a failed generate."""
        assert _stage_model_names([None, 42, "str", {}, {"outputs": ["x"]}]) == {}
        assert _stage_model_names(None) == {}
        assert _stage_model_names([]) == {}


class TestCollidingOutputsLoseNoSql:
    """Two stages may legitimately declare the same output -- an incremental
    stage and its backfill twin, say. Emitted files are keyed by path, so
    handing both the output name would silently drop one stage's SQL: the
    exact failure this rename exists to end, reintroduced by the fix."""

    CONTRACT = {"metadata": {"name": "x"}}

    def _stages(self):
        return [
            {
                "name": "stg_a",
                "outputs": ["orders"],
                "properties": {"sql": "select 1 as a"},
            },
            {
                "name": "stg_b",
                "outputs": ["orders"],
                "properties": {"sql": "select 2 as b"},
            },
        ]

    def test_both_stages_still_emit_a_file(self):
        files = _generate_multi_stage(self.CONTRACT, {"properties": {"stages": self._stages()}})
        assert sorted(files) == [
            "models/staging/stg_a.sql",
            "models/staging/stg_b.sql",
        ]

    def test_neither_stage_loses_its_sql(self):
        files = _generate_multi_stage(self.CONTRACT, {"properties": {"stages": self._stages()}})
        body = "\n".join(files.values())
        assert "select 1 as a" in body
        assert "select 2 as b" in body

    def test_contested_stages_keep_their_own_names(self):
        assert _stage_model_names(self._stages()) == {
            "stg_a": "stg_a",
            "stg_b": "stg_b",
        }

    def test_an_output_colliding_with_another_stages_name_is_contested_too(self):
        """`orders` is claimed by the stage called `orders` (no outputs, so it
        keeps its name) and by `stg_x`'s output. Counting declared outputs
        alone would miss this one."""
        assert _stage_model_names(
            [{"name": "orders"}, {"name": "stg_x", "outputs": ["orders"]}]
        ) == {"orders": "orders", "stg_x": "stg_x"}

    def test_the_collision_is_logged_not_swallowed(self, caplog):
        with caplog.at_level("WARNING"):
            _stage_model_names(self._stages())
        assert "dbt_stage_model_name_collision" in caplog.text
        assert "stg_a" in caplog.text and "stg_b" in caplog.text

    def test_an_uncontested_stage_alongside_a_contested_one_still_renames(self):
        stages = self._stages() + [{"name": "stg_c", "outputs": ["customers"]}]
        assert _stage_model_names(stages)["stg_c"] == "customers"
