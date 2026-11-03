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

"""``lifecycle.expire`` on Databricks: accepted for a table rebuilt in full, Delta history limited.

Pure functions only. The spec is the module docstring of
``iac/providers/databricks_retention.py``: a dbt ``table`` model or forge-cli's DuckDB
acquisition rewrites the whole table, so ``expire`` is accepted when the contract's retention
covers the Delta history (30 days by default); anything else is refused.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.iac import IAC_PLUGINS
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.providers import databricks_retention as ret
from fluid_build.iac.source import contract_source

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

WAREHOUSE = "wh-0123456789abcdef"
DAY = 86400


# ── durations ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,seconds",
    [
        ("P30D", 30 * DAY),
        ("P365D", 365 * DAY),
        ("P6Y", 6 * 365 * DAY),
        ("P1Y", 365 * DAY),  # the shortest year: never read as longer than it is
        ("P1M", 28 * DAY),  # the shortest month
        ("P2M", 56 * DAY),
        ("P1Y2M3W4D", (365 + 56 + 21 + 4) * DAY),
        ("P4W", 28 * DAY),
        ("PT720H", 30 * DAY),
        ("PT36H", 36 * 3600),
        ("P1DT12H30M15S", DAY + 12 * 3600 + 30 * 60 + 15),
        ("P0D", 0),
        ("PT0S", 0),
        (" P7D ", 7 * DAY),
    ],
)
def test_iso_duration(text, seconds):
    assert ret.iso_duration_seconds(text) == seconds


@pytest.mark.parametrize(
    "text",
    [
        "",
        "P",
        "PT",
        "P1DT",  # a T with no time part
        "30 days",
        "p30d",
        "P-1D",
        "P1.5D",
        "P1,5D",
        "P1H",  # hours need the T
        "PT1D",
        "P1D1Y",  # out of order
        "P" + "9" * 13 + "D",
        None,
        30,
    ],
)
def test_iso_duration_rejects(text):
    with pytest.raises(ValueError):
        ret.iso_duration_seconds(text)


@pytest.mark.parametrize(
    "text,seconds",
    [
        ("interval 30 days", 30 * DAY),
        ("INTERVAL 7 DAYS", 7 * DAY),
        ("interval 1 week", 7 * DAY),
        ("interval 1 week 12 hours", 7 * DAY + 12 * 3600),
        ("interval 48 hours", 48 * 3600),
        ("30 days", 30 * DAY),
        ("interval 1500 milliseconds", 2),  # rounded up, never down
    ],
)
def test_interval(text, seconds):
    assert ret.interval_seconds(text) == seconds


@pytest.mark.parametrize(
    "text", ["", "interval", "interval 1 month", "interval 1 year", "interval -1 days", "soon", 5]
)
def test_interval_rejects(text):
    with pytest.raises(ValueError):
        ret.interval_seconds(text)


def test_history_is_the_larger_of_the_two_properties_and_defaults_to_30_days():
    assert ret.history_seconds({}) == 30 * DAY
    assert ret.history_seconds({ret.LOG_RETENTION: "interval 10 days"}) == 10 * DAY
    assert ret.history_seconds({ret.LOG_RETENTION: "interval 2 days"}) == 7 * DAY
    assert ret.history_seconds({ret.DELETED_FILE_RETENTION: "interval 90 days"}) == 90 * DAY
    # names match case-insensitively
    assert ret.history_seconds({"DELTA.LOGRETENTIONDURATION": "interval 400 days"}) == 400 * DAY
    with pytest.raises(ValueError):
        ret.history_seconds({ret.LOG_RETENTION: "forever"})


# ── the contract ────────────────────────────────────────────────────────────
def _set(target: Any, path: str, value: Any) -> None:
    keys = path.split(".")
    for key in keys[:-1]:
        target = target[int(key)] if isinstance(target, list) else target.setdefault(key, {})
    if isinstance(target, list):
        target[int(keys[-1])] = value
    else:
        target[keys[-1]] = value


def _contract(**overrides: Any) -> Dict[str, Any]:
    """``orders`` on Databricks with ``expire`` after P365D, written by a local dbt build."""
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "sales.orders",
        "name": "Orders",
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {"database": "main", "schema": "sales", "table": "orders"},
                },
                "lifecycle": {"retention": "P365D", "expire": True},
                "contract": {"schema": [{"name": "id", "type": "bigint"}]},
            }
        ],
        "builds": [
            {
                "id": "transform",
                "engine": "dbt",
                "repository": "./dbt",
                "outputs": ["orders"],
                "properties": {"model": "orders"},
                "execution": {"runtime": {"platform": "databricks"}},
            }
        ],
    }
    for path, value in overrides.items():
        _set(contract, path.replace("__", "."), value)
    return contract


def _acquisition(**build: Any) -> Dict[str, Any]:
    return {
        "id": "land",
        "engine": "duckdb",
        "pattern": "acquisition",
        "outputs": ["orders"],
        **build,
    }


def _project(
    tmp_path: Path,
    sql: str = "select 1 as id\n",
    *,
    project_models: str = "",
    yaml_config: Optional[str] = None,
    name: str = "orders",
    folder: str = "",
    extra: Optional[Dict[str, str]] = None,
) -> Path:
    root = tmp_path / "dbt"
    root.mkdir(exist_ok=True)
    (root / "dbt_project.yml").write_text(f"name: shop\n{project_models}", encoding="utf-8")
    models = root / "models" / folder
    models.mkdir(parents=True, exist_ok=True)
    (models / f"{name}.sql").write_text(sql, encoding="utf-8")
    if yaml_config is not None:
        (models / "schema.yml").write_text(
            f"models:\n  - name: {name}\n    config:\n{yaml_config}", encoding="utf-8"
        )
    for rel, text in (extra or {}).items():
        path = root / "models" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _compile(
    contract: Dict[str, Any], tmp_path: Optional[Path] = None, *, validating: bool = False
) -> dbx._Compiled:
    source = tmp_path / "contract.fluid.yaml" if tmp_path is not None else None
    with contract_source(source):
        return dbx._compile(contract, {dbx.WAREHOUSE_ENV: WAREHOUSE}, validating=validating)


def _kinds(compiled: dbx._Compiled) -> List[str]:
    return [refusal.kind for refusal in compiled.refusals]


def _accepted(compiled: dbx._Compiled) -> dbx._Table:
    assert not compiled.refusals, [f"{r.kind}: {r}" for r in compiled.refusals]
    (table,) = compiled.tables
    assert table.retention is not None
    return table


def _refused(compiled: dbx._Compiled, kind: str) -> UnsupportedBindingError:
    assert f"databricks-retention-{kind}" in _kinds(compiled), _kinds(compiled)
    assert not compiled.tables, "a refused expose emits no table"
    return next(r for r in compiled.refusals if r.kind == f"databricks-retention-{kind}")


# ── a dbt model that is rebuilt in full ─────────────────────────────────────
class TestDbtTable:
    def test_sql_config_table_is_accepted(self, tmp_path):
        _project(tmp_path, "{{ config(materialized='table') }}\nselect 1 as id\n")
        table = _accepted(_compile(_contract(), tmp_path))
        assert table.retention.period == "P365D" and table.retention.seconds == 365 * DAY

    def test_double_quotes_whitespace_control_and_other_args(self, tmp_path):
        _project(
            tmp_path,
            '{{- config(\n  materialized="table",\n  partition_by=["id"],\n  tags=["a"]\n) -}}\n'
            "select 1 as id\n",
        )
        _accepted(_compile(_contract(), tmp_path))

    def test_yaml_config_table_is_accepted(self, tmp_path):
        _project(tmp_path, yaml_config="      materialized: table\n")
        _accepted(_compile(_contract(), tmp_path))

    def test_project_config_with_the_plus_prefix(self, tmp_path):
        _project(tmp_path, project_models="models:\n  shop:\n    +materialized: table\n")
        _accepted(_compile(_contract(), tmp_path))

    def test_project_config_without_the_plus_prefix(self, tmp_path):
        _project(tmp_path, project_models="models:\n  shop:\n    materialized: table\n")
        _accepted(_compile(_contract(), tmp_path))

    def test_root_level_project_config(self, tmp_path):
        _project(tmp_path, project_models="models:\n  +materialized: table\n")
        _accepted(_compile(_contract(), tmp_path))

    def test_folder_config(self, tmp_path):
        _project(
            tmp_path,
            folder="marts",
            project_models="models:\n  shop:\n    +materialized: view\n    marts:\n"
            "      +materialized: table\n",
        )
        _accepted(_compile(_contract(), tmp_path))

    def test_config_named_for_the_model_itself(self, tmp_path):
        _project(
            tmp_path,
            folder="marts",
            project_models="models:\n  shop:\n    marts:\n      +materialized: view\n"
            "      orders:\n        +materialized: table\n",
        )
        _accepted(_compile(_contract(), tmp_path))

    # dbt: config() in the model > the model's YAML config > dbt_project.yml, and the
    # most specific path in dbt_project.yml.
    @pytest.mark.parametrize(
        "project,yaml,sql,accepted",
        [
            ("incremental", "table", None, True),  # YAML over the project
            ("table", "incremental", None, False),
            ("view", "view", "table", True),  # SQL over both
            ("table", "table", "incremental", False),
            ("incremental", "incremental", "table", True),
            (None, "view", "table", True),
            (None, "table", "view", False),
        ],
    )
    def test_precedence(self, tmp_path, project, yaml, sql, accepted):
        _project(
            tmp_path,
            (f"{{{{ config(materialized='{sql}') }}}}\n" if sql else "") + "select 1 as id\n",
            project_models=(f"models:\n  shop:\n    +materialized: {project}\n" if project else ""),
            yaml_config=f"      materialized: {yaml}\n" if yaml else None,
        )
        compiled = _compile(_contract(), tmp_path)
        if accepted:
            _accepted(compiled)
        else:
            assert compiled.refusals and not compiled.tables

    def test_the_more_specific_project_path_wins(self, tmp_path):
        _project(
            tmp_path,
            folder="marts",
            project_models="models:\n  shop:\n    marts:\n      +materialized: incremental\n"
            "  +materialized: table\n",
        )
        _refused(_compile(_contract(), tmp_path), "incremental")

    def test_later_config_call_wins(self, tmp_path):
        _project(
            tmp_path,
            "{{ config(materialized='incremental') }}\n{{ config(materialized='table') }}\n"
            "select 1 as id\n",
        )
        _accepted(_compile(_contract(), tmp_path))

    def test_a_commented_out_or_raw_config_is_not_read(self, tmp_path):
        _project(
            tmp_path,
            "{# {{ config(materialized='incremental') }} #}\n"
            "{% raw %}{{ config(materialized='incremental') }}{% endraw %}\n"
            "{{ config(materialized='table') }}\nselect 1 as id\n",
        )
        _accepted(_compile(_contract(), tmp_path))

    def test_the_model_is_found_by_alias(self, tmp_path):
        _project(
            tmp_path,
            "{{ config(materialized='table', alias='orders') }}\nselect 1 as id\n",
            name="stg_orders_final",
        )
        # dbt selects by the model's name (its file), not its alias.
        contract = _contract(builds__0__properties={"select": "stg_orders_final"})
        _accepted(_compile(contract, tmp_path))
        _refused(_compile(_contract(), tmp_path), "not-selected")

    def test_the_model_is_the_one_named_for_the_table_not_its_neighbours(self, tmp_path):
        _project(
            tmp_path,
            "{{ config(materialized='table') }}\nselect 1 as id\n",
            extra={"other.sql": "{{ config(materialized='incremental') }}\nselect 1\n"},
        )
        _accepted(_compile(_contract(), tmp_path))

    def test_a_table_is_found_case_insensitively(self, tmp_path):
        _project(tmp_path, "{{ config(materialized='table') }}\nselect 1 as id\n", name="Orders")
        # The relation is found without case; dbt's selection is case-sensitive.
        _accepted(_compile(_contract(builds__0__properties={"model": "Orders"}), tmp_path))
        _refused(_compile(_contract(), tmp_path), "not-selected")


class TestDbtRefused:
    @pytest.mark.parametrize("kind", ["incremental", "snapshot"])
    def test_a_model_that_keeps_old_rows(self, tmp_path, kind):
        _project(tmp_path, f"{{{{ config(materialized='{kind}') }}}}\nselect 1 as id\n")
        refusal = _refused(_compile(_contract(), tmp_path), "incremental")
        assert kind in str(refusal) and "materialized='table'" in " ".join(refusal.remediation)

    @pytest.mark.parametrize("kind", ["view", "ephemeral", "materialized_view", "Table", "x"])
    def test_a_model_that_is_not_a_table(self, tmp_path, kind):
        _project(tmp_path, f"{{{{ config(materialized='{kind}') }}}}\nselect 1 as id\n")
        _refused(_compile(_contract(), tmp_path), "not-table")

    def test_dbts_default_is_a_view(self, tmp_path):
        _project(tmp_path)
        refusal = _refused(_compile(_contract(), tmp_path), "not-table")
        assert "view" in str(refusal)

    @pytest.mark.parametrize(
        "sql",
        [
            "{{ config(materialized=var('m')) }}\nselect 1",
            "{{ config(materialized=mat) }}\nselect 1",
            "{% if var('x') %}{{ config(materialized='table') }}{% endif %}\nselect 1",
            "{% for x in y %}{{ config(materialized='table') }}{% endfor %}\nselect 1",
            "{{ config(**opts) }}\nselect 1",
            "{{ config('table') }}\nselect 1",
            "{{ config(materialized='table' }}\nselect 1",
            "{{ config(schema='other', materialized='table') }}\nselect 1",
            "{{ config(database='other', materialized='table') }}\nselect 1",
            "{{ config(enabled=false, materialized='table') }}\nselect 1",
        ],
    )
    def test_what_cannot_be_determined_is_refused_with_the_fix(self, tmp_path, sql):
        _project(tmp_path, sql)
        refusal = _refused(_compile(_contract(), tmp_path), "dbt-unresolved")
        assert "materialized='table'" in " ".join(refusal.remediation)

    def test_a_config_after_a_closed_block_is_still_read(self, tmp_path):
        _project(
            tmp_path,
            "{% set x = 1 %}{% if x %}{% endif %}{{ config(materialized='table') }}\nselect 1",
        )
        _accepted(_compile(_contract(), tmp_path))

    def test_a_project_jinja_value_is_not_a_literal(self, tmp_path):
        _project(
            tmp_path,
            project_models="models:\n  shop:\n    +materialized: \"{{ var('m') }}\"\n",
        )
        _refused(_compile(_contract(), tmp_path), "dbt-unresolved")

    def test_a_git_repository_is_not_readable_here(self, tmp_path):
        contract = _contract(builds__0__repository="https://github.com/o/r?tag=v1&path=dbt")
        refusal = _refused(_compile(contract, tmp_path), "dbt-unresolved")
        assert "Git repository" in str(refusal) and "?tag" not in str(refusal)
        assert "local dbt project" in " ".join(refusal.remediation)

    def test_no_project_at_the_repository(self, tmp_path):
        _refused(_compile(_contract(), tmp_path), "dbt-unresolved")

    def test_no_model_creates_the_table(self, tmp_path):
        _project(tmp_path, "{{ config(materialized='table') }}\nselect 1", name="customers")
        refusal = _refused(_compile(_contract(), tmp_path), "dbt-unresolved")
        assert "'orders'" in str(refusal)

    def test_two_models_creating_it(self, tmp_path):
        _project(
            tmp_path,
            "{{ config(materialized='table') }}\nselect 1",
            extra={"b.sql": "{{ config(alias='orders') }}\nselect 1"},
        )
        _refused(_compile(_contract(), tmp_path), "dbt-unresolved")

    def test_an_alias_that_is_not_a_literal_cannot_be_ruled_out(self, tmp_path):
        _project(
            tmp_path,
            "{{ config(materialized='table') }}\nselect 1",
            extra={"b.sql": "{{ config(alias=var('a')) }}\nselect 1"},
        )
        _refused(_compile(_contract(), tmp_path), "dbt-unresolved")

    def test_a_python_model(self, tmp_path):
        _project(tmp_path, name="other", extra={"orders.py": "def model(dbt, s): pass\n"})
        _refused(_compile(_contract(), tmp_path), "dbt-unresolved")

    def test_a_model_path_outside_the_project(self, tmp_path):
        _project(tmp_path, "{{ config(materialized='table') }}\nselect 1")
        (tmp_path / "dbt" / "dbt_project.yml").write_text(
            "name: shop\nmodel-paths: ['../x']\n", encoding="utf-8"
        )
        _refused(_compile(_contract(), tmp_path), "dbt-unresolved")

    def test_a_broken_dbt_project_yml(self, tmp_path):
        _project(tmp_path)
        (tmp_path / "dbt" / "dbt_project.yml").write_text("name: [", encoding="utf-8")
        _refused(_compile(_contract(), tmp_path), "dbt-unresolved")

    def test_a_linked_model_is_not_followed(self, tmp_path):
        _project(tmp_path, name="other")
        outside = tmp_path / "outside.sql"
        outside.write_text("{{ config(materialized='table') }}\nselect 1", encoding="utf-8")
        (tmp_path / "dbt" / "models" / "orders.sql").symlink_to(outside)
        _refused(_compile(_contract(), tmp_path), "dbt-unresolved")

    def test_a_model_path_through_a_linked_folder_is_not_followed(self, tmp_path):
        _project(tmp_path, name="other")
        outside = tmp_path / "outside" / "sub"
        outside.mkdir(parents=True)
        (outside / "orders.sql").write_text(
            "{{ config(materialized='table') }}\nselect 1", encoding="utf-8"
        )
        (tmp_path / "dbt" / "linked").symlink_to(tmp_path / "outside")
        (tmp_path / "dbt" / "dbt_project.yml").write_text(
            "name: shop\nmodel-paths: ['linked/sub']\n", encoding="utf-8"
        )
        _refused(_compile(_contract(), tmp_path), "dbt-unresolved")

    def test_validate_without_a_contract_file_reads_nothing(self):
        # As for the job emitter: nothing to read yet, so nothing refused for the project.
        _accepted(_compile(_contract(), None, validating=True))
        # The emit path knows no location, and says so.
        _refused(_compile(_contract(), None), "dbt-unresolved")


def _selecting(properties: Dict[str, Any], **overrides: Any) -> Dict[str, Any]:
    return _contract(builds__0__properties=properties, **overrides)


class TestSelection:
    """The guarantee needs the build to run the model: its ``--select`` must include it."""

    @pytest.fixture(autouse=True)
    def _project(self, tmp_path):
        _project(
            tmp_path,
            "{{ config(materialized='table') }}\nselect 1 as id\n",
            folder="marts",
            extra={"other.sql": "select 1\n"},
        )

    @pytest.mark.parametrize(
        "properties",
        [
            {"model": "orders"},  # +orders+
            {"select": "orders"},
            {"select": ["orders"]},
            {"select": "orders+"},
            {"select": "+orders"},
            {"select": "2+orders"},
            {"select": "orders+3"},
            {"select": "@orders"},
            {"models": "orders"},
            {"select": "fqn:orders"},
            {"select": "model:orders"},
            {"select": "shop.marts.orders"},
            {"select": "shop.marts"},
            {"select": "shop.*"},
            {"select": "ord*"},
            {"select": "*"},
            {"select": "path:models/marts"},
            {"select": "path:models/marts/"},
            {"select": "models/marts/orders.sql"},
            {"select": ["other", "orders"]},
            {"select": "other orders"},
            {"select": "orders,path:models/marts"},
            {"select": "+orders+,shop.marts"},
            {},  # no selector: dbt builds everything
            {"model": "other"},  # outputs > 1 : no selector, the whole project
        ],
    )
    def test_a_selection_that_includes_the_model_is_accepted(self, tmp_path, properties):
        contract = _selecting(properties)
        if properties == {"model": "other"}:
            contract["builds"][0]["outputs"] = ["orders", "other"]
            contract["exposes"].append(copy.deepcopy(contract["exposes"][0]))
            contract["exposes"][1].update(exposeId="other")
            contract["exposes"][1]["binding"]["location"]["table"] = "other"
            contract["exposes"][1].pop("lifecycle")
        compiled = _compile(contract, tmp_path)
        assert not compiled.refusals, [f"{r.kind}: {r}" for r in compiled.refusals]

    @pytest.mark.parametrize(
        "properties",
        [
            {"select": "other"},
            {"select": "customers"},
            {"select": "shop.orders"},  # dbt's fqn is a prefix: marts is skipped
            {"select": "models"},  # no slash: a name, not the path
            {"select": ["other", "customers"]},
            {"select": "shop.other"},
            {"select": "path:models/staging"},
            {"select": "models/other.sql"},
            {"select": "orders,other"},  # intersection of two names
            {"select": "stg_*"},
        ],
    )
    def test_a_selection_that_leaves_it_out_is_refused(self, tmp_path, properties):
        refusal = _refused(_compile(_selecting(properties), tmp_path), "not-selected")
        assert "'orders'" in str(refusal) and "model 'orders'" in str(refusal)
        assert "does not select it" in str(refusal)
        assert "properties.select" in " ".join(refusal.remediation)

    @pytest.mark.parametrize(
        "properties",
        [
            {"select": "tag:nightly"},
            {"select": "config.materialized:table"},
            {"select": "tag:nightly,orders"},
            {"model": "other"},  # +other+
            {"select": "+other"},  # other's ancestors: only dbt's graph knows
            {"select": "other+"},
            {"select": "result:error"},
        ],
    )
    def test_a_selection_only_the_graph_or_state_can_answer_is_refused(self, tmp_path, properties):
        refusal = _refused(_compile(_selecting(properties), tmp_path), "not-selected")
        assert "cannot evaluate" in str(refusal)

    def test_a_flag_like_selector_is_dropped_as_the_runner_drops_it(self, tmp_path):
        # build_selectors removes it, leaving no selector: the same set dbt would run.
        _accepted_or_not = _compile(_selecting({"select": ["--full-refresh"]}), tmp_path)
        assert not _accepted_or_not.refusals

    def test_the_selection_is_named_in_the_message(self, tmp_path):
        refusal = _refused(
            _compile(_selecting({"select": ["other", "tag:x"]}), tmp_path), "not-selected"
        )
        assert "other tag:x" in str(refusal)

    def test_not_selected_is_checked_before_the_materialization(self, tmp_path):
        _project(tmp_path, "select 1\n", folder="marts")  # a view, and not selected
        compiled = _compile(_selecting({"select": "other"}), tmp_path)
        assert "databricks-retention-not-selected" in _kinds(compiled)
        assert "databricks-retention-not-table" not in _kinds(compiled)

    def test_an_acquisition_has_no_dbt_selection(self):
        _accepted(_compile(_contract(builds=[_acquisition(properties={"select": "other"})])))


class TestDeltaProperties:
    def _table(self, tmp_path, props: str, retention: str = "P365D"):
        _project(
            tmp_path,
            "{{ config(materialized='table', tblproperties=" + props + ") }}\nselect 1 as id\n",
        )
        return _compile(
            _contract(exposes__0__lifecycle={"retention": retention, "expire": True}), tmp_path
        )

    def test_a_longer_history_than_the_contract_is_refused(self, tmp_path):
        compiled = self._table(tmp_path, "{'delta.logRetentionDuration': 'interval 400 days'}")
        refusal = _refused(compiled, "too-short")
        assert "400 days" in str(refusal) and "declared properties" in str(refusal)

    def test_a_history_inside_the_contract_is_accepted(self, tmp_path):
        compiled = self._table(
            tmp_path,
            "{'delta.logRetentionDuration': 'interval 400 days',"
            " 'delta.deletedFileRetentionDuration': 'interval 7 days'}",
            "P2Y",
        )
        _accepted(compiled)

    def test_the_deleted_file_retention_counts_too(self, tmp_path):
        compiled = self._table(
            tmp_path, "{'delta.deletedFileRetentionDuration': 'interval 500 days'}"
        )
        _refused(compiled, "too-short")

    def test_a_property_that_is_not_an_interval_is_refused(self, tmp_path):
        compiled = self._table(tmp_path, "{'delta.logRetentionDuration': 'a long time'}")
        _refused(compiled, "dbt-unresolved")


# ── the retention itself ────────────────────────────────────────────────────
class TestPeriod:
    @pytest.fixture(autouse=True)
    def _dbt_table(self, tmp_path):
        _project(tmp_path, "{{ config(materialized='table') }}\nselect 1 as id\n")

    @pytest.mark.parametrize("period", ["P30D", "P31D", "P365D", "P6Y", "P1Y", "P2M", "PT720H"])
    def test_a_retention_that_covers_the_history_is_accepted(self, tmp_path, period):
        contract = _contract(exposes__0__lifecycle={"retention": period, "expire": True})
        _accepted(_compile(contract, tmp_path))

    @pytest.mark.parametrize("period", ["P7D", "P1D", "P29D", "P4W", "P1M", "PT719H", "P0D"])
    def test_a_retention_shorter_than_30_days_is_refused(self, tmp_path, period):
        contract = _contract(exposes__0__lifecycle={"retention": period, "expire": True})
        refusal = _refused(_compile(contract, tmp_path), "too-short")
        assert "30 days" in str(refusal) and "P30D" in " ".join(refusal.remediation)

    @pytest.mark.parametrize("period", ["30 days", "P", "p30d", "P1.5D", 30, "P1DT"])
    def test_a_retention_that_is_not_an_iso_duration_is_refused(self, tmp_path, period):
        contract = _contract(exposes__0__lifecycle={"retention": period, "expire": True})
        _refused(_compile(contract, tmp_path), "period")

    def test_expire_without_a_retention_is_refused(self, tmp_path):
        contract = _contract(exposes__0__lifecycle={"expire": True})
        _refused(_compile(contract, tmp_path), "period")

    @pytest.mark.parametrize(
        "lifecycle", [{"retention": "P7D"}, {"retention": "P7D", "expire": False}, {}]
    )
    def test_without_expire_nothing_is_checked(self, tmp_path, lifecycle):
        compiled = _compile(_contract(exposes__0__lifecycle=lifecycle), tmp_path)
        assert not compiled.refusals and compiled.tables[0].retention is None


# ── the writers ─────────────────────────────────────────────────────────────
class TestWriters:
    def test_a_duckdb_acquisition_is_accepted(self):
        contract = _contract(builds=[_acquisition()])
        assert _accepted(_compile(contract)).retention.seconds == 365 * DAY

    def test_an_acquisition_without_outputs_is_still_the_writer(self):
        build = _acquisition()
        del build["outputs"]
        _accepted(_compile(_contract(builds=[build])))

    def test_an_acquisition_with_a_short_retention_is_still_refused(self):
        contract = _contract(
            builds=[_acquisition()], exposes__0__lifecycle={"retention": "P7D", "expire": True}
        )
        _refused(_compile(contract), "too-short")

    @pytest.mark.parametrize(
        "build",
        [
            {"engine": "dlt", "pattern": "acquisition"},
            {"engine": "airbyte", "pattern": "acquisition"},
            {"engine": "duckdb", "pattern": "embedded-logic"},
            {"engine": "python"},
            {"engine": "spark"},
            {"engine": "flink"},
        ],
    )
    def test_a_writer_not_known_to_rewrite_the_table_is_refused(self, build):
        contract = _contract(builds=[{"id": "w", "outputs": ["orders"], **build}])
        refusal = _refused(_compile(contract), "unmapped")
        assert "orders" in str(refusal) or "build 'w'" in str(refusal)

    def test_no_build_writes_it(self):
        contract = _contract(builds=[])
        _refused(_compile(contract), "unmapped")
        contract = _contract(builds=[{**_acquisition(), "outputs": ["something_else"]}])
        _refused(_compile(contract), "unmapped")

    def test_every_writer_must_rewrite_the_table(self, tmp_path):
        contract = _contract(
            builds=[_acquisition(), {"id": "w", "engine": "python", "outputs": ["orders"]}]
        )
        _refused(_compile(contract, tmp_path), "unmapped")

    def test_a_dbt_writer_beside_an_acquisition(self, tmp_path):
        _project(tmp_path, "{{ config(materialized='incremental') }}\nselect 1 as id\n")
        contract = _contract()
        contract["builds"].insert(0, _acquisition())
        _refused(_compile(contract, tmp_path), "incremental")


# ── validate and the emitter are one pass ───────────────────────────────────
CASES = {
    "table": ("{{ config(materialized='table') }}\nselect 1 as id\n", {}, None),
    "incremental": ("{{ config(materialized='incremental') }}\nselect 1\n", {}, "incremental"),
    "view": ("select 1\n", {}, "not-table"),
    "var": ("{{ config(materialized=var('m')) }}\nselect 1\n", {}, "dbt-unresolved"),
    "short": (
        "{{ config(materialized='table') }}\nselect 1\n",
        {"exposes__0__lifecycle": {"retention": "P7D", "expire": True}},
        "too-short",
    ),
}


@pytest.mark.parametrize("label", sorted(CASES))
def test_validate_and_the_emitter_agree(tmp_path, monkeypatch, label):
    sql, overrides, kind = CASES[label]
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)
    _project(tmp_path, sql)
    contract = _contract(**overrides)
    with contract_source(tmp_path / "contract.fluid.yaml"):
        errors, _warnings = dbx.validate_databricks_binding(copy.deepcopy(contract))
        if kind is None:
            assert not errors
            assert "databricks_sql_table" in IAC_PLUGINS["databricks"].emit(contract)
            return
        assert errors
        with pytest.raises(UnsupportedBindingError) as exc:
            IAC_PLUGINS["databricks"].emit(contract)
    assert exc.value.kind == f"databricks-retention-{kind}"
    assert str(exc.value) in errors[0]


def test_managed_objects_carry_the_accepted_retention(tmp_path):
    _project(tmp_path, "{{ config(materialized='table') }}\nselect 1 as id\n")
    with contract_source(tmp_path / "contract.fluid.yaml"):
        tables, _jobs = dbx.managed_objects(_contract(), {dbx.WAREHOUSE_ENV: WAREHOUSE})
    (table,) = tables
    assert (table.retention_period, table.retention_seconds) == ("P365D", 365 * DAY)
    plain = _contract(exposes__0__lifecycle={})
    with contract_source(tmp_path / "contract.fluid.yaml"):
        (table,), _ = dbx.managed_objects(plain, {dbx.WAREHOUSE_ENV: WAREHOUSE})
    assert table.retention_seconds is None
