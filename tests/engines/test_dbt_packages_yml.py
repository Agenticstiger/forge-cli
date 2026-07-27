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

"""packages.yml emission — generated dbt projects declare the packages they use.

Pins the card's acceptance criteria:

* A project whose tests reference ``dbt_utils.`` / ``dbt_expectations.``
  namespaces gets a ``packages.yml`` with hub pins, so ``dbt deps`` +
  ``dbt parse`` pass out of the box.
* A plain not_null/unique-only project gets NO ``packages.yml``.
* ``--mesh-hub`` (which emits ``dependencies.yml``) folds the pins into that
  file instead — dbt forbids ``packages.yml`` + ``dependencies.yml`` coexisting.
* A user-managed (sentinel-less) ``packages.yml`` on disk is never clobbered.
* ``dbt_utils.recency`` derives datepart/interval from the contract's ISO
  freshness window (no hardcoded 1-day, no non-dbt ``_fluid_window`` kwarg).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import yaml

from fluid_build.engines.dbt import DbtEngine
from fluid_build.engines.dbt import packages_yml as pkg

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _contract(*, dq_rules: list | None = None, schema: list | None = None) -> Dict[str, Any]:
    """Minimal generate-able dbt contract with injectable quality intent."""
    contract_block: Dict[str, Any] = {
        "schema": schema
        or [
            {"name": "id", "type": "STRING", "required": True},
            {"name": "amount", "type": "NUMBER"},
        ]
    }
    if dq_rules is not None:
        contract_block["dq"] = {"rules": dq_rules}
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "gold.analytics.orders_v1",
        "name": "Orders",
        "builds": [
            {
                "id": "main",
                "engine": "dbt",
                "pattern": "hybrid-reference",
                "execution": {"runtime": {"platform": "local"}},
            }
        ],
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "contract": contract_block,
            }
        ],
    }


_RANGE_SCHEMA = [
    {"name": "id", "type": "STRING", "required": True},
    {"name": "amount", "type": "NUMBER", "minimum": 0, "maximum": 100},
]

_FRESHNESS_RULE = {
    "id": "fresh",
    "type": "freshness",
    "selector": "updated_at",
    "window": "PT6H",
    "severity": "warn",
}


def _generate(contract: Dict[str, Any], **kwargs: Any) -> Dict[str, str]:
    return DbtEngine().generate(contract, contract["builds"][0], **kwargs)


# ---------------------------------------------------------------------------
# required_packages — the needed-only gate
# ---------------------------------------------------------------------------


class TestRequiredPackages:
    def test_plain_tests_need_no_packages(self):
        files = {"models/marts/schema.yml": "tests:\n- not_null\n- unique\n"}
        assert pkg.required_packages(files) == []

    def test_range_test_needs_dbt_expectations_only(self):
        files = {"schema.yml": "- dbt_expectations.expect_column_values_to_be_between:\n"}
        assert pkg.required_packages(files) == ["dbt_expectations"]

    def test_recency_needs_dbt_utils_only(self):
        files = {"schema.yml": "- dbt_utils.recency:\n    field: updated_at\n"}
        assert pkg.required_packages(files) == ["dbt_utils"]

    def test_model_sql_macros_count_too(self):
        # User SQL calling dbt_utils macros needs the package as much as a
        # test does — the scan covers every emitted file, not just YAML.
        files = {"models/marts/dim.sql": "{{ dbt_utils.generate_surrogate_key(['id']) }}"}
        assert pkg.required_packages(files) == ["dbt_utils"]


# ---------------------------------------------------------------------------
# render_packages_yml — hub schema + sentinel
# ---------------------------------------------------------------------------


class TestRender:
    def test_shape_matches_dbt_hub_schema(self):
        content = pkg.render_packages_yml(["dbt_utils", "dbt_expectations"])
        assert content.startswith(pkg.MANAGED_BY_SENTINEL)
        doc = yaml.safe_load(content)
        assert doc == {
            "packages": [
                {"package": "dbt-labs/dbt_utils", "version": [">=1.4.0", "<1.5.0"]},
                # dbt_expectations maintenance moved calogica → Metaplane;
                # the hub's current releases live under the metaplane org.
                {"package": "metaplane/dbt_expectations", "version": [">=0.10.0", "<0.11.0"]},
            ]
        }

    def test_sentinel_matches_exporter_constant(self):
        # packages_yml can't import the exporter's constant (circular import
        # through engines.dbt.__init__) so the literal is duplicated — this
        # pin keeps the two byte-identical.
        from fluid_build.exporters.dbt_tests import MANAGED_BY_SENTINEL

        assert pkg.MANAGED_BY_SENTINEL == MANAGED_BY_SENTINEL


# ---------------------------------------------------------------------------
# Engine emission — needed-only, mesh fold-in, user-file safety
# ---------------------------------------------------------------------------


class TestEngineEmission:
    def test_range_and_freshness_project_gets_both_pins(self):
        contract = _contract(dq_rules=[_FRESHNESS_RULE], schema=_RANGE_SCHEMA)
        files = _generate(contract)
        assert "packages.yml" in files
        doc = yaml.safe_load(files["packages.yml"])
        names = [p["package"] for p in doc["packages"]]
        assert names == ["dbt-labs/dbt_utils", "metaplane/dbt_expectations"]

    def test_plain_not_null_project_gets_no_packages_yml(self):
        """Acceptance: a plain not_null-only contract gets no packages.yml."""
        files = _generate(_contract())
        assert "packages.yml" not in files

    def test_mesh_hub_folds_pins_into_dependencies_yml(self):
        """dbt forbids packages.yml + dependencies.yml coexisting — when
        --mesh-hub emitted dependencies.yml the pins ride along in its
        ``packages:`` key instead."""
        contract = _contract(dq_rules=[_FRESHNESS_RULE], schema=_RANGE_SCHEMA)
        files = _generate(contract, mesh_hub="central_hub")
        assert "packages.yml" not in files
        doc = yaml.safe_load(files["dependencies.yml"])
        assert doc["projects"] == [{"name": "central_hub"}]
        names = [p["package"] for p in doc["packages"]]
        assert "dbt-labs/dbt_utils" in names
        assert "metaplane/dbt_expectations" in names

    def test_existing_user_managed_packages_yml_left_untouched(self, tmp_path, caplog):
        user_content = "packages:\n  - package: dbt-labs/dbt_utils\n    version: 1.0.0\n"
        (tmp_path / "packages.yml").write_text(user_content, encoding="utf-8")
        contract = _contract(dq_rules=[_FRESHNESS_RULE], schema=_RANGE_SCHEMA)
        with caplog.at_level(logging.WARNING, logger="fluid_build.engines.dbt.packages_yml"):
            files = _generate(contract, output_dir=tmp_path)
        assert "packages.yml" not in files  # never clobbered by the write loop
        assert (tmp_path / "packages.yml").read_text(encoding="utf-8") == user_content
        # ...and the user is told which pins the project needs.
        assert any("packages_yml_left_untouched" in r.message for r in caplog.records)
        assert any("dbt-labs/dbt_utils" in r.getMessage() for r in caplog.records)

    def test_existing_fluid_managed_packages_yml_is_regenerated(self, tmp_path):
        (tmp_path / "packages.yml").write_text(
            f"{pkg.MANAGED_BY_SENTINEL}\npackages: []\n", encoding="utf-8"
        )
        contract = _contract(dq_rules=[_FRESHNESS_RULE], schema=_RANGE_SCHEMA)
        files = _generate(contract, output_dir=tmp_path)
        assert "packages.yml" in files
        assert pkg.MANAGED_BY_SENTINEL in files["packages.yml"]

    def test_merge_preserves_existing_dependency_packages(self):
        content = (
            "projects:\n- name: hub\npackages:\n- package: dbt-labs/dbt_utils\n  version: 1.2.0\n"
        )
        merged = yaml.safe_load(pkg.merge_into_dependencies_yml(content, ["dbt_utils"]))
        # Existing pin wins on collision — no duplicate entry appended.
        assert merged["packages"] == [{"package": "dbt-labs/dbt_utils", "version": "1.2.0"}]


# ---------------------------------------------------------------------------
# Recency window derivation (the bonus fix, now in _test_mapping.recency_test)
# ---------------------------------------------------------------------------


class TestRecencyWindow:
    @staticmethod
    def _model_recency(files):
        """The ``dbt_utils.recency`` entry on the emitted *model*.

        Recency is a model-level test: dbt injects ``column_name`` into every
        generic test reached through ``columns[].tests``, and the
        ``dbt_utils`` macro accepts no such kwarg — attaching it to a column
        made the whole project unparseable.
        """
        doc = yaml.safe_load(files["models/marts/schema.yml"])
        model = doc["models"][0]
        assert not any(
            "dbt_utils.recency" in t
            for col in model.get("columns", [])
            for t in col.get("tests", [])
            if isinstance(t, dict)
        ), "recency must never attach to a column"
        return next(t for t in model["tests"] if isinstance(t, dict) and "dbt_utils.recency" in t)[
            "dbt_utils.recency"
        ]

    def test_window_drives_datepart_and_interval(self):
        """Acceptance: recency derives from the contract's freshness rule,
        not a hardcoded 1-day — and measures the column the rule selected."""
        contract = _contract(dq_rules=[_FRESHNESS_RULE])  # PT6H
        body = self._model_recency(_generate(contract))
        assert body == {"field": "updated_at", "datepart": "hour", "interval": 6}

    def test_missing_window_falls_back_to_one_day(self):
        rule = {"id": "f", "type": "freshness", "selector": "updated_at"}
        body = self._model_recency(_generate(_contract(dq_rules=[rule])))
        assert body["field"] == "updated_at"
        assert body["datepart"] == "day"
        assert body["interval"] == 1


# ---------------------------------------------------------------------------
# CLI mirror — `fluid generate dbt-tests` emits packages.yml alongside
# ---------------------------------------------------------------------------


class TestCliMirror:
    def _run(self, tmp_path, contract: Dict[str, Any]) -> int:
        import argparse

        from fluid_build.cli.generate_dbt_tests import run

        contract_path = tmp_path / "contract.fluid.yaml"
        contract_path.write_text(yaml.safe_dump(contract), encoding="utf-8")
        args = argparse.Namespace(
            contract=str(contract_path), out=str(tmp_path / "schema.yml"), env=None
        )
        return run(args, logging.getLogger("test"))

    def _freshness_contract(self) -> Dict[str, Any]:
        return {
            "exposes": [
                {
                    "exposeId": "orders",
                    "binding": {"location": {"table": "ORDERS"}},
                    "contract": {
                        "schema": [{"name": "updated_at", "type": "TIMESTAMP"}],
                        "dq": {"rules": [dict(_FRESHNESS_RULE)]},
                    },
                }
            ]
        }

    def test_writes_packages_yml_next_to_schema_yml(self, tmp_path):
        assert self._run(tmp_path, self._freshness_contract()) == 0
        content = (tmp_path / "packages.yml").read_text(encoding="utf-8")
        assert pkg.MANAGED_BY_SENTINEL in content
        assert "dbt-labs/dbt_utils" in content

    def test_no_packages_yml_when_not_needed(self, tmp_path):
        contract = {
            "exposes": [
                {
                    "exposeId": "orders",
                    "binding": {"location": {"table": "ORDERS"}},
                    "contract": {"schema": [{"name": "id", "type": "STRING", "required": True}]},
                }
            ]
        }
        assert self._run(tmp_path, contract) == 0
        assert not (tmp_path / "packages.yml").exists()

    def test_user_managed_packages_yml_untouched(self, tmp_path):
        user_content = "packages: []\n"
        (tmp_path / "packages.yml").write_text(user_content, encoding="utf-8")
        assert self._run(tmp_path, self._freshness_contract()) == 0
        assert (tmp_path / "packages.yml").read_text(encoding="utf-8") == user_content
