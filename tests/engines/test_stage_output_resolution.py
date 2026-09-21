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

"""The stage -> output mapping both engines resolve through.

`resolve_stage_outputs` returns `Optional[str]`, and the Optional is the
point: the two engines need the absence expressed differently. A dbt model
is a file and always needs *some* name, so `stage_model_names` falls back to
the stage's own; the sql engine must never invent a view name, so `None`
means "emit a bare SELECT".

`tests/engines/test_dbt_models_named_after_outputs.py` pins what the dbt
emitter does with this. This file pins the resolver itself.
"""

from __future__ import annotations

import pytest

from fluid_build.engines._stages import resolve_stage_outputs, stage_model_names


class TestTheOptionalContract:
    """What separates this from the dbt-facing shim."""

    def test_a_stage_with_an_output_owns_it(self):
        assert resolve_stage_outputs([{"name": "s", "outputs": ["o"]}]) == {"s": "o"}

    def test_a_stage_without_one_resolves_to_none_not_its_own_name(self):
        assert resolve_stage_outputs([{"name": "s"}]) == {"s": None}

    def test_the_dbt_shim_substitutes_the_stage_name_for_none(self):
        assert stage_model_names([{"name": "s"}]) == {"s": "s"}

    def test_the_two_agree_wherever_an_output_exists(self):
        stages = [{"name": "a", "outputs": ["x"]}, {"name": "b", "outputs": ["y"]}]
        resolved = resolve_stage_outputs(stages)
        assert stage_model_names(stages) == {k: v for k, v in resolved.items()}


class TestFirstUsableOutputWins:
    @pytest.mark.parametrize(
        "stage,expected",
        [
            ({"name": "s", "outputs": ["o"]}, "o"),
            ({"name": "s", "outputs": ["first", "second"]}, "first"),
            ({"name": "s", "outputs": []}, None),
            ({"name": "s"}, None),
            ({"name": "s", "outputs": None}, None),
            ({"name": "s", "outputs": [None, "later"]}, "later"),
            ({"name": "s", "outputs": ["", "later"]}, "later"),
            ({"name": "s", "outputs": [123, "later"]}, "later"),
        ],
        ids=[
            "one",
            "first-wins",
            "empty-list",
            "absent",
            "explicit-null",
            "skips-none",
            "skips-blank",
            "skips-non-string",
        ],
    )
    def test_cases(self, stage, expected):
        assert resolve_stage_outputs([stage]) == {"s": expected}


class TestContestedNamesGoToNobody:
    """Callers key a file path or a catalog relation by this name, so two
    stages claiming one would silently drop one of them."""

    def test_two_stages_declaring_the_same_output(self):
        assert resolve_stage_outputs(
            [{"name": "a", "outputs": ["o"]}, {"name": "b", "outputs": ["o"]}]
        ) == {"a": None, "b": None}

    def test_contest_is_judged_on_effective_names_not_declared_outputs(self):
        """The stage literally called `orders` declares no outputs, so it
        would fall back to `orders` and collide with the other stage's
        output. Counting declared outputs alone misses this entirely."""
        assert resolve_stage_outputs(
            [{"name": "orders"}, {"name": "x", "outputs": ["orders"]}]
        ) == {"orders": None, "x": None}

    def test_three_way_contest(self):
        stages = [{"name": n, "outputs": ["o"]} for n in ("a", "b", "c")]
        assert resolve_stage_outputs(stages) == {"a": None, "b": None, "c": None}

    def test_an_uncontested_stage_alongside_a_contested_one_keeps_its_output(self):
        """Guards against an over-broad fix that stops resolving entirely
        once any collision exists."""
        stages = [
            {"name": "a", "outputs": ["o"]},
            {"name": "b", "outputs": ["o"]},
            {"name": "c", "outputs": ["z"]},
        ]
        assert resolve_stage_outputs(stages)["c"] == "z"

    def test_the_collision_is_logged_with_the_stages_involved(self, caplog):
        with caplog.at_level("WARNING"):
            resolve_stage_outputs(
                [{"name": "a", "outputs": ["o"]}, {"name": "b", "outputs": ["o"]}]
            )
        assert "stage_output_name_collision" in caplog.text
        assert "'a'" in caplog.text and "'b'" in caplog.text

    def test_the_log_event_is_caller_selectable(self, caplog):
        """The dbt engine keeps its historical event name so the CI log
        parsers and its own pinned test keep matching."""
        with caplog.at_level("WARNING"):
            stage_model_names([{"name": "a", "outputs": ["o"]}, {"name": "b", "outputs": ["o"]}])
        assert "dbt_stage_model_name_collision" in caplog.text

    def test_no_collision_logs_nothing(self, caplog):
        with caplog.at_level("WARNING"):
            resolve_stage_outputs([{"name": "a", "outputs": ["o"]}])
        assert "collision" not in caplog.text


class TestMalformedInputIsSkippedNotRaised:
    """This runs before anything is emitted; raising would turn a naming
    improvement into a failed generate."""

    @pytest.mark.parametrize(
        "stages",
        [None, [], [None], [42], ["a string"], [{}], [{"outputs": ["x"]}], [{"name": ""}]],
        ids=[
            "none",
            "empty",
            "null-entry",
            "int-entry",
            "str-entry",
            "empty-dict",
            "no-name",
            "blank-name",
        ],
    )
    def test_yields_an_empty_mapping(self, stages):
        assert resolve_stage_outputs(stages) == {}

    def test_good_entries_survive_alongside_bad_ones(self):
        assert resolve_stage_outputs([None, {"name": "ok", "outputs": ["o"]}, 42, {}]) == {
            "ok": "o"
        }

    def test_a_non_string_stage_name_is_stringified(self):
        assert resolve_stage_outputs([{"name": 5, "outputs": ["o"]}]) == {"5": "o"}
