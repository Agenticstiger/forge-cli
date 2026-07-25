# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""dbt manifest.json importer tests.

Fixtures under ``tests/cli/fixtures/dbt_manifests/`` are hand-crafted,
modeled on ``dbt parse`` output of dbt-core's jaffle-shop demo (manifest
schema v12, dbt 1.8.x, snowflake adapter), reduced to the fields the
importer consumes. v10/v11 are minimal duckdb variants; v8 exists only to
exercise the minimum-schema-version gate.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.cli.import_workflow import DbtManifestImporter, get_importer
from fluid_build.schema_manager import FluidSchemaManager

FIXTURES = Path(__file__).parent / "fixtures" / "dbt_manifests"


@pytest.fixture(scope="module")
def schema_manager() -> FluidSchemaManager:
    return FluidSchemaManager()


@pytest.fixture(scope="module")
def v12_contract() -> tuple[Dict[str, Any], Any]:
    return DbtManifestImporter().import_to_contract(str(FIXTURES / "manifest_v12.json"))


def _expose(contract: Dict[str, Any], expose_id: str) -> Dict[str, Any]:
    return next(e for e in contract["exposes"] if e["exposeId"] == expose_id)


def _column(expose: Dict[str, Any], name: str) -> Dict[str, Any]:
    return next(c for c in expose["contract"]["schema"] if c["name"] == name)


def _project_dir(tmp_path: Path, with_catalog: bool = True) -> Path:
    """Materialize the v12 fixture as a real dbt project layout."""
    project = tmp_path / "jaffle_shop"
    (project / "target").mkdir(parents=True)
    (project / "models").mkdir()
    (project / "dbt_project.yml").write_text("name: jaffle_shop\nversion: '1.0'\n")
    shutil.copy(FIXTURES / "manifest_v12.json", project / "target" / "manifest.json")
    if with_catalog:
        shutil.copy(FIXTURES / "catalog_v12.json", project / "target" / "catalog.json")
    return project


# ── Registry ────────────────────────────────────────────────────────────


class TestRegistry:
    def test_dbt_importer_registered(self):
        assert get_importer("dbt") is not None

    def test_can_import_project_dir_and_manifest_path(self, tmp_path: Path):
        importer = DbtManifestImporter()
        project = _project_dir(tmp_path)
        assert importer.can_import(str(project))
        assert importer.can_import(str(project / "target" / "manifest.json"))
        assert not importer.can_import(str(tmp_path / "not-a-project"))


# ── v12 happy path — faithful brownfield conversion ─────────────────────


class TestV12Import:
    def test_no_five_model_cap(self, v12_contract):
        contract, _ = v12_contract
        # 6 models + 1 seed survive (ephemeral + disabled + foreign-package don't).
        assert len(contract["exposes"]) == 7 > 5
        assert {e["exposeId"] for e in contract["exposes"]} == {
            "stg_orders",
            "stg_customers",
            "stg_payments",
            "orders",
            "customers",
            "order_payments",
            "raw_country_codes",
        }

    def test_layer_and_product_type_from_folders(self, v12_contract):
        contract, _ = v12_contract
        # marts present → most-downstream layer wins: Gold ↔ CDP.
        assert contract["metadata"]["layer"] == "Gold"
        assert contract["metadata"]["productType"] == "CDP"
        assert contract["id"] == "gold.jaffle_shop"
        assert _expose(contract, "stg_orders")["labels"]["dbt-layer"] == "staging"
        assert _expose(contract, "orders")["labels"]["dbt-layer"] == "marts"

    def test_columns_typed_from_manifest_data_type(self, v12_contract):
        contract, _ = v12_contract
        stg_orders = _expose(contract, "stg_orders")
        assert _column(stg_orders, "order_id")["type"] == "number(38,0)"
        assert _column(stg_orders, "status")["type"] == "varchar"  # character varying
        assert _column(stg_orders, "loaded_at")["type"] == "timestamptz"
        # Unmappable warehouse type honestly defaults to string.
        assert _column(stg_orders, "geo_cell")["type"] == "string"

    def test_ref_derived_lineage_recorded(self, v12_contract):
        contract, _ = v12_contract
        orders = _expose(contract, "orders")
        assert orders["labels"]["dbt-depends-on"] == "stg_orders,int_order_payments"
        build = contract["builds"][0]
        assert build["engine"] == "dbt"
        step = next(t for t in build["transformations"] if t["name"] == "orders")
        assert step["model"] == "models/marts/orders.sql"
        assert step["outputs"] == ["orders"]
        assert set(build["outputs"]) == {e["exposeId"] for e in contract["exposes"]}

    def test_materialized_to_physical_hints(self, v12_contract):
        contract, _ = v12_contract
        assert _expose(contract, "stg_orders")["kind"] == "view"
        assert _expose(contract, "orders")["kind"] == "table"
        materializations = contract["builds"][0]["properties"]["materializations"]
        assert materializations["orders"] == "table"
        assert materializations["stg_orders"] == "view"
        assert materializations["order_payments"] == "incremental"
        binding = _expose(contract, "orders")["binding"]
        assert binding["platform"] == "snowflake"
        assert binding["format"] == "snowflake_table"
        assert binding["location"] == {
            "database": "ANALYTICS",
            "schema": "JAFFLE",
            "table": "orders",
        }
        assert _expose(contract, "stg_orders")["binding"]["format"] == "snowflake_view"

    def test_ephemeral_disabled_and_foreign_models_excluded(self, v12_contract):
        contract, report = v12_contract
        expose_ids = {e["exposeId"] for e in contract["exposes"]}
        assert "int_order_payments" not in expose_ids  # ephemeral
        assert "deprecated_orders" not in expose_ids  # disabled
        assert "audit_helper" not in expose_ids  # foreign package
        assert any("deprecated_orders" in u for u in report.unsupported)
        assert any("ephemeral" in n for n in report.notes)
        assert any("dbt_audit_pkg" in n for n in report.notes)


# ── tests → dq.rules[] via the SHARED reverse table ─────────────────────


class TestDqRuleRecovery:
    def test_rules_recovered_through_shared_reverse_table(self, v12_contract):
        contract, _ = v12_contract
        rules = _expose(contract, "stg_orders")["contract"]["dq"]["rules"]
        by_type = {r["type"]: r for r in rules}
        assert by_type["completeness"]["selector"] == "order_id"  # not_null
        assert by_type["uniqueness"]["selector"] == "order_id"  # unique
        assert by_type["valid_values"]["selector"] == "status"  # accepted_values
        # dbt severity WARN survives.
        assert by_type["valid_values"]["severity"] == "warn"
        assert by_type["completeness"]["severity"] == "error"

    def test_valid_values_description_round_trips(self, v12_contract):
        """The emitted description is the exact shape _test_mapping parses back."""
        import fluid_build.engines.dbt._test_mapping as _tm

        contract, _ = v12_contract
        rules = _expose(contract, "stg_orders")["contract"]["dq"]["rules"]
        rule = next(r for r in rules if r["type"] == "valid_values")
        assert _tm.valid_values(rule) == ["placed", "shipped", "completed", "returned"]

    def test_recency_maps_to_freshness_with_iso_window(self, v12_contract):
        contract, _ = v12_contract
        rules = _expose(contract, "orders")["contract"]["dq"]["rules"]
        rule = next(r for r in rules if r["type"] == "freshness")
        assert rule["window"] == "P1D"  # datepart=day interval=1
        assert rule["selector"] == "most_recent_order_at"

    def test_expression_is_true_maps_to_accuracy(self, v12_contract):
        contract, _ = v12_contract
        rules = _expose(contract, "order_payments")["contract"]["dq"]["rules"]
        rule = next(r for r in rules if r["type"] == "accuracy")
        assert "amount >= 0" in rule["description"]

    def test_importer_consumes_the_shared_hook_not_a_private_table(self, monkeypatch):
        """Patching _test_mapping.test_to_rule_type must flow through — proves
        the importer reuses the shared module rather than a 4th mapper."""
        import fluid_build.engines.dbt._test_mapping as _tm

        monkeypatch.setattr(_tm, "REVERSE_TEST_TO_RULE", {"not_null": "uniqueness"})
        contract, _ = DbtManifestImporter().import_to_contract(str(FIXTURES / "manifest_v11.json"))
        rules = _expose(contract, "items")["contract"]["dq"]["rules"]
        assert [r["type"] for r in rules] == ["uniqueness"]  # remapped via the table

    def test_unknown_and_singular_tests_reported_unsupported(self, v12_contract):
        _, report = v12_contract
        assert any("is_even" in u for u in report.unsupported)
        assert any("assert_positive_totals" in u for u in report.unsupported)


# ── PK / FK / range recovery (datacontract-cli borrow) ──────────────────


class TestConstraintRecovery:
    def test_pk_from_unique_plus_not_null_tests(self, v12_contract):
        contract, _ = v12_contract
        col = _column(_expose(contract, "stg_orders"), "order_id")
        assert col["semanticType"] == "identifier"
        assert col["required"] is True

    def test_pk_from_model_level_constraints(self, tmp_path):
        contract, _ = DbtManifestImporter().import_to_contract(str(_project_dir(tmp_path)))
        col = _column(_expose(contract, "customers"), "customer_id")
        assert col["semanticType"] == "identifier"

    def test_fk_recovered_from_relationships_test(self, v12_contract):
        contract, report = v12_contract
        col = _column(_expose(contract, "orders"), "customer_id")
        assert col["validationRules"] == [
            {
                "type": "custom",
                "constraint": "references customers.customer_id",
                "message": "customer_id must exist in customers.customer_id",
            }
        ]
        assert any("FK recovered" in n for n in report.notes)

    def test_range_recovered_from_between_test(self, v12_contract):
        contract, _ = v12_contract
        col = _column(_expose(contract, "order_payments"), "amount")
        assert col["validationRules"] == [{"type": "range", "constraint": ">= 0 and <= 100000"}]


# ── sources → consumes[] ────────────────────────────────────────────────


class TestConsumes:
    def test_sources_become_consumes(self, v12_contract):
        contract, _ = v12_contract
        consumes = {(c["productId"], c["exposeId"]): c for c in contract["consumes"]}
        assert set(consumes) == {
            ("source.raw_jaffle", "orders"),
            ("source.raw_jaffle", "customers"),
            ("source.raw_jaffle", "payments"),
        }

    def test_source_freshness_maps_to_qos(self, v12_contract):
        contract, _ = v12_contract
        by_id = {c["exposeId"]: c for c in contract["consumes"]}
        # error_after takes precedence over warn_after.
        assert by_id["orders"]["qosExpectations"] == {"freshnessMax": "PT24H"}
        # warn_after fallback when error_after absent.
        assert by_id["payments"]["qosExpectations"] == {"freshnessMax": "P1D"}
        assert "qosExpectations" not in by_id["customers"]  # no freshness config


# ── catalog.json overlay ────────────────────────────────────────────────


class TestCatalogOverlay:
    def test_catalog_types_win_case_insensitively(self, tmp_path):
        contract, _ = DbtManifestImporter().import_to_contract(str(_project_dir(tmp_path)))
        orders = _expose(contract, "orders")
        # Manifest had data_type=None; Snowflake catalog names are UPPERCASE.
        assert _column(orders, "order_id")["type"] == "number(38,0)"
        assert _column(orders, "most_recent_order_at")["type"] == "timestamp_ntz"
        assert _column(_expose(contract, "customers"), "full_name")["type"] == "varchar(255)"

    def test_catalog_only_columns_are_added(self, tmp_path):
        contract, _ = DbtManifestImporter().import_to_contract(str(_project_dir(tmp_path)))
        names = [c["name"] for c in _expose(contract, "customers")["contract"]["schema"]]
        assert "SEGMENT" in names  # present only in catalog.json

    def test_without_catalog_types_default_and_are_reported(self, v12_contract):
        contract, report = v12_contract
        assert _column(_expose(contract, "orders"), "order_id")["type"] == "string"
        assert any("catalog.json not found" in d for d in report.required_defaults)
        assert any("orders.order_id" in d for d in report.required_defaults)


# ── schema-version matrix + gate ────────────────────────────────────────


class TestVersionMatrix:
    @pytest.mark.parametrize("fixture", ["manifest_v10.json", "manifest_v11.json"])
    def test_v10_v11_parse_and_map(self, fixture):
        contract, _ = DbtManifestImporter().import_to_contract(str(FIXTURES / fixture))
        assert {e["exposeId"] for e in contract["exposes"]} == {"stg_items", "items"}
        # v10 fixture has no attached_node — depends_on fallback must resolve it.
        rules = _expose(contract, "items")["contract"]["dq"]["rules"]
        assert [r["type"] for r in rules] == ["completeness"]
        assert contract["consumes"][0]["productId"] == "source.raw"

    def test_v8_rejected_by_min_version_gate(self):
        with pytest.raises(ValueError, match="v8.*minimum.*v9"):
            DbtManifestImporter().import_to_contract(str(FIXTURES / "manifest_v8_too_old.json"))

    def test_missing_manifest_advises_dbt_parse(self, tmp_path):
        (tmp_path / "dbt_project.yml").write_text("name: empty\n")
        with pytest.raises(FileNotFoundError, match="dbt parse"):
            DbtManifestImporter().import_to_contract(str(tmp_path))


# ── every emitted contract passes fluid validate ────────────────────────


class TestEveryContractValidates:
    @pytest.mark.parametrize(
        "fixture", ["manifest_v10.json", "manifest_v11.json", "manifest_v12.json"]
    )
    def test_schema_valid_direct_manifest(self, fixture, schema_manager):
        contract, _ = DbtManifestImporter().import_to_contract(str(FIXTURES / fixture))
        result = schema_manager.validate_contract(contract, "0.7.3", offline_only=True)
        assert result.is_valid, result.errors

    def test_schema_valid_project_dir_with_catalog(self, tmp_path, schema_manager):
        contract, _ = DbtManifestImporter().import_to_contract(str(_project_dir(tmp_path)))
        result = schema_manager.validate_contract(contract, "0.7.3", offline_only=True)
        assert result.is_valid, result.errors

    def test_fluid_validate_cli_passes_end_to_end(self, tmp_path):
        """The real ``fluid validate`` accepts the emitted YAML."""
        contract, _ = DbtManifestImporter().import_to_contract(str(_project_dir(tmp_path)))
        out = tmp_path / "contract.fluid.yaml"
        out.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "fluid_build.cli", "validate", str(out)],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr


# ── legacy directory-scan routing (single dbt path, no fidelity split) ──


def _scan_args(target_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        tool=None,
        source=None,
        out_path=None,
        provider="local",
        target_dir=str(target_dir),
        yes=True,
    )


class TestLegacyScanRouting:
    def test_scan_routes_to_manifest_importer_when_manifest_exists(self, tmp_path):
        from fluid_build.cli import import_cmd

        project = _project_dir(tmp_path)
        rc = import_cmd.run(_scan_args(project), logging.getLogger("test"))
        assert rc == 0
        # The manifest importer wrote its contract into the scanned dir …
        written = project / "contract.gold.jaffle_shop.fluid.yaml"
        assert written.exists()
        contract = yaml.safe_load(written.read_text(encoding="utf-8"))
        # … with manifest fidelity (typed columns), not the regex scanner's toys.
        orders = _expose(contract, "orders")
        assert _column(orders, "order_id")["type"] == "number(38,0)"

    def test_explicit_tool_mode_writes_contract(self, tmp_path, monkeypatch):
        from fluid_build.cli._import_workflow_handler import run_import_from_tool

        project = _project_dir(tmp_path)
        monkeypatch.chdir(tmp_path)
        args = argparse.Namespace(out_path=str(tmp_path / "out.fluid.yaml"))
        rc = run_import_from_tool(args, logging.getLogger("test"), tool="dbt", source=str(project))
        assert rc == 0
        assert (tmp_path / "out.fluid.yaml").exists()

    def test_scan_without_manifest_falls_back_to_regex_scanner(self, tmp_path, capsys):
        from fluid_build.cli import import_cmd

        project = tmp_path / "no-manifest"
        (project / "models").mkdir(parents=True)
        (project / "dbt_project.yml").write_text("name: bare_project\nversion: '1.0'\n")
        (project / "models" / "orders.sql").write_text("SELECT order_id, status FROM raw.orders\n")
        rc = import_cmd.run(_scan_args(project), logging.getLogger("test"))
        assert rc == 0
        # No manifest-importer artifact; the advisory told the user about dbt parse.
        assert not list(project.glob("contract.gold.*"))
        # Rich line-wraps the advisory — compare whitespace-normalized.
        flat = " ".join(capsys.readouterr().out.split())
        assert "dbt parse" in flat


# ── ImportReport accounting ─────────────────────────────────────────────


class TestImportReport:
    def test_every_model_and_source_accounted(self, v12_contract):
        contract, report = v12_contract
        for expose in contract["exposes"]:
            uid_name = expose["labels"]["dbt-unique-id"].rsplit(".", 1)[-1]
            assert f"model.{uid_name}" in report.mapped_one_to_one
        for consume in contract["consumes"]:
            assert any(
                consume["exposeId"] in item and item.endswith("→ consumes[]")
                for item in report.mapped_one_to_one
            )

    def test_every_mapped_test_accounted(self, v12_contract):
        contract, report = v12_contract
        emitted_rules = sum(
            len(e["contract"].get("dq", {}).get("rules", [])) for e in contract["exposes"]
        )
        mapped_tests = [i for i in report.mapped_one_to_one if i.startswith("test.")]
        assert emitted_rules == len(mapped_tests) == 5

    def test_defaults_and_boundary_note_present(self, v12_contract):
        _, report = v12_contract
        assert any("metadata.owner defaulted" in d for d in report.required_defaults)
        assert any("ONE DataProduct per dbt project" in n for n in report.notes)


class TestSemanticLayerImport:
    """manifest semantic_models + metrics → exposes[].semantics.

    Round-trip closure: the MetricFlow bridge EXPORTS the semantics
    block; before this leg a brownfield dbt project lost its semantic
    layer on import.
    """

    def _orders_semantics(self) -> Dict[str, Any]:
        contract, _ = DbtManifestImporter().import_to_contract(str(FIXTURES / "manifest_v12.json"))
        expose = _expose(contract, "orders")
        assert "semantics" in expose, "orders semantic model must attach to the orders expose"
        return expose["semantics"]

    def test_semantic_model_attaches_to_the_right_expose(self) -> None:
        semantics = self._orders_semantics()
        assert semantics["name"] == "orders"
        assert semantics["description"] == "Order fact semantic model"
        assert semantics["defaultAggTimeDimension"] == "most_recent_order_at"

    def test_entities_map_with_unsupported_type_dropped(self) -> None:
        entities = {e["name"]: e for e in self._orders_semantics()["entities"]}
        assert entities["order"] == {"name": "order", "type": "primary", "expr": "order_id"}
        assert entities["customer"]["type"] == "foreign"
        assert "shard" not in entities  # type: hyperscale — dropped with a note

    def test_dimensions_map_and_granularity_normalizes_or_omits(self) -> None:
        dimensions = {d["name"]: d for d in self._orders_semantics()["dimensions"]}
        assert dimensions["most_recent_order_at"]["type"] == "time"
        assert dimensions["most_recent_order_at"]["typeParams"] == {"timeGranularity": "day"}
        # nanosecond has no contract equivalent — granularity omitted, dimension kept
        assert dimensions["loaded_microbatch"]["type"] == "time"
        assert "typeParams" not in dimensions["loaded_microbatch"]
        assert dimensions["customer_id"]["type"] == "categorical"

    def test_measures_map_with_extras_and_unsupported_agg_dropped(self) -> None:
        measures = {m["name"]: m for m in self._orders_semantics()["measures"]}
        assert measures["revenue"]["agg"] == "sum"
        assert measures["revenue"]["expr"] == "order_total"
        assert measures["revenue"]["createMetric"] is True
        assert measures["last_order_total"]["nonAdditiveDimension"] == {
            "name": "most_recent_order_at",
            "windowChoice": "max",
        }
        # percentile measure survives; its agg_params are reported, not emitted
        assert measures["p95_order_value"]["agg"] == "percentile"
        assert "aggParams" not in measures["p95_order_value"]
        assert "weird" not in measures  # agg: hyperloglog — dropped

    def test_metrics_map_simple_ratio_derived_and_skip_cumulative(self) -> None:
        metrics = {m["name"]: m for m in self._orders_semantics()["metrics"]}
        assert metrics["total_revenue"]["type"] == "simple"
        assert metrics["total_revenue"]["measure"] == "revenue"
        assert metrics["total_revenue"]["filter"] == "order_total > 0"
        assert metrics["aov"] == {
            "name": "aov",
            "type": "ratio",
            "numerator": "revenue",
            "denominator": "order_count",
        }
        assert metrics["revenue_growth"]["type"] == "derived"
        assert metrics["revenue_growth"]["inputMetrics"] == ["total_revenue"]
        assert "rolling_28d" not in metrics  # cumulative — no contract slot yet

    def test_losses_are_reported_not_silent(self) -> None:
        _, report = DbtManifestImporter().import_to_contract(str(FIXTURES / "manifest_v12.json"))
        text = "\n".join(report.unsupported)
        assert "ghost" in text  # semantic model on an ephemeral model
        assert "agg_params" in text
        assert "window_groupings" in text
        assert "cumulative" in text
        assert "hyperscale" in text or "shard" in text

    def test_imported_contract_still_passes_schema_validation(self, schema_manager) -> None:
        contract, _ = DbtManifestImporter().import_to_contract(str(FIXTURES / "manifest_v12.json"))
        result = schema_manager.validate_contract(contract, "0.7.3", offline_only=True)
        assert result.is_valid, result.errors


class TestGovernanceMetaRoundTrip:
    """owner / tags / labels ride into dbt as namespaced config.meta (the
    MetricFlow bridge side) — the importer recovers them so the contract's
    governance surface survives contract → dbt → contract."""

    def test_semantic_model_tags_and_labels_recovered(self) -> None:
        contract, _ = DbtManifestImporter().import_to_contract(str(FIXTURES / "manifest_v12.json"))
        semantics = _expose(contract, "orders")["semantics"]
        assert semantics["tags"] == ["certified"]
        assert semantics["labels"] == {"tier": "gold"}

    def test_metric_owner_recovered(self) -> None:
        contract, _ = DbtManifestImporter().import_to_contract(str(FIXTURES / "manifest_v12.json"))
        semantics = _expose(contract, "orders")["semantics"]
        total_revenue = next(m for m in semantics["metrics"] if m["name"] == "total_revenue")
        assert total_revenue["owner"] == "finance-team"

    def test_contract_with_recovered_governance_still_schema_valid(self, schema_manager) -> None:
        contract, _ = DbtManifestImporter().import_to_contract(str(FIXTURES / "manifest_v12.json"))
        result = schema_manager.validate_contract(contract, "0.7.3", offline_only=True)
        assert result.is_valid, result.errors


class TestJinjaHardening:
    """A hostile third-party manifest can carry Jinja in free-text fields;
    dbt renders Jinja in YAML on `dbt parse`, so importing → generating →
    parsing would leak env vars into the operator's artifact. Display fields
    are stripped; SQL-bearing fields (expr/filter) are preserved but flagged
    for review, with legitimate MetricFlow templates recognised."""

    def _import_hostile(self):
        return DbtManifestImporter().import_to_contract(
            str(FIXTURES / "manifest_v12_jinja_hostile.json")
        )

    def _orders(self, contract) -> Dict[str, Any]:
        return _expose(contract, "orders")["semantics"]

    def test_display_fields_are_stripped_of_jinja(self) -> None:
        contract, _ = self._import_hostile()
        sem = self._orders(contract)
        assert "{{" not in sem["description"] and "env_var" not in sem["description"]
        # tags/labels scrubbed
        assert all("{{" not in t for t in sem["tags"])
        assert "certified" in sem["tags"]  # clean sibling preserved
        # the fully-templated label scrubbed away; the clean sibling survived
        assert all("{{" not in v for v in sem["labels"].values())
        assert sem["labels"] == {"domain": "commerce"}
        # nested descriptions scrubbed
        entity = next(e for e in sem["entities"] if e["name"] == "order")
        assert "{{" not in entity["description"]
        measure = next(m for m in sem["measures"] if m["name"] == "revenue")
        assert "{%" not in measure["description"] and "{{" not in measure["description"]

    def test_metric_owner_and_description_stripped(self) -> None:
        contract, _ = self._import_hostile()
        total = next(m for m in self._orders(contract)["metrics"] if m["name"] == "total_revenue")
        assert "{{" not in total["owner"] and "env_var" not in total["owner"]
        assert "{{" not in total["description"]

    def test_hostile_expr_preserved_but_flagged(self) -> None:
        """A hostile measure expr is SQL-bearing so it is kept verbatim, but
        the operator gets a REVIEW-before-generate warning."""
        contract, report = self._import_hostile()
        measure = next(m for m in self._orders(contract)["measures"] if m["name"] == "revenue")
        assert measure["expr"] == "amount + {{ env_var('BONUS') }}"  # preserved
        text = "\n".join(report.unsupported)
        assert "REVIEW before generating" in text
        assert "measure revenue expr" in text

    def test_metricflow_filter_template_preserved_and_noted_not_flagged(self) -> None:
        """A legitimate {{ Dimension(...) }} filter is expected templating —
        preserved, noted, NOT surfaced as a review risk."""
        contract, report = self._import_hostile()
        total = next(m for m in self._orders(contract)["metrics"] if m["name"] == "total_revenue")
        assert total["filter"] == "{{ Dimension('order__status') }} = 'completed'"
        assert any("MetricFlow object templating" in n for n in report.notes)

    def test_hostile_filter_env_var_is_flagged(self) -> None:
        contract, report = self._import_hostile()
        leaky = next(m for m in self._orders(contract)["metrics"] if m["name"] == "leaky")
        assert leaky["filter"] == "amount > {{ env_var('SECRET') }}"  # preserved
        text = "\n".join(report.unsupported)
        assert "metric leaky filter" in text and "REVIEW before generating" in text

    def test_hardened_contract_still_schema_valid(self, schema_manager) -> None:
        contract, _ = self._import_hostile()
        result = schema_manager.validate_contract(contract, "0.7.3", offline_only=True)
        assert result.is_valid, result.errors

    def test_no_rendered_env_var_survives_anywhere_in_contract(self) -> None:
        """Belt-and-suspenders: no imported display field anywhere in the
        contract still carries a strippable Jinja span (SQL exprs/filters are
        the only allowed carriers, and those are flagged)."""
        import json as _json

        contract, _ = self._import_hostile()
        semantics_json = _json.dumps([e.get("semantics", {}) for e in contract["exposes"]])
        # env_var may survive ONLY inside expr/filter values (SQL-bearing).
        sem = self._orders(contract)
        allowed = {sem["measures"][0].get("expr", "")}
        for m in sem["metrics"]:
            allowed.add(m.get("filter", ""))
        leaked = "env_var" in semantics_json
        if leaked:
            # every env_var occurrence must be inside an allowed SQL carrier
            residual = semantics_json
            for a in allowed:
                residual = residual.replace(_json.dumps(a)[1:-1], "")
            assert "env_var" not in residual, "env_var leaked into a non-SQL field"


class TestJinjaScrubIsReformationProof:
    """A single regex pass can reform a delimiter at a gap junction
    (e.g. {{%%}%set x=env_var('S')%} -> {%set x=env_var('S')%}), which
    would then EXECUTE on dbt parse. The scrub loops to a fixpoint +
    strips stray delimiter fragments, so no Jinja delimiter can survive."""

    _DELIM = __import__("re").compile(r"\{\{|\}\}|\{%|%\}|\{#|#\}")

    @__import__("pytest").mark.parametrize(
        "hostile",
        [
            "{{%%}%set q=env_var('S')%}#",  # reforms {% %} after one pass
            "{{{{ x }}}}",  # leaves dangling }} without cleanup
            "{{ '{{' }}",
            "{ {{ } }}",
            "{{ }}{{ }}",
            "{%{% set x=1 %}%}",  # nested statement tags
            "{#{# c #}#}",
            "{{-  env_var('S')  -}}",  # whitespace-control
            "{% set x = env_var('SECRET') %}{{ x }}",
        ],
    )
    def test_no_delimiter_survives_reformation(self, hostile):
        from fluid_build.cli.import_workflow.dbt import _scrub_display_text
        from fluid_build.cli.import_workflow.registry import ImportReport

        out = _scrub_display_text(hostile, field_desc="d", report=ImportReport())
        assert not self._DELIM.findall(out), f"delimiter survived: {out!r}"
        assert "env_var" not in out and "SECRET" not in out

    def test_single_braces_are_legitimate_display_text(self):
        from fluid_build.cli.import_workflow.dbt import _scrub_display_text
        from fluid_build.cli.import_workflow.registry import ImportReport

        report = ImportReport()
        assert (
            _scrub_display_text("use {value} here", field_desc="d", report=report)
            == "use {value} here"
        )
        assert not report.unsupported  # no Jinja -> no redaction note


# ── --split-by: product-boundary control (folder | group) ────────────────


MULTIDOMAIN = "manifest_v12_multidomain.json"


def _split_import(split_by: str) -> tuple[list, Any]:
    return DbtManifestImporter().import_to_contracts(
        str(FIXTURES / MULTIDOMAIN), options={"split_by": split_by}
    )


def _by_id(contracts: list) -> Dict[str, Dict[str, Any]]:
    return {c["id"]: c for c in contracts}


class TestSplitByFolder:
    @pytest.fixture(scope="class")
    def folder_result(self) -> tuple[list, Any]:
        return _split_import("folder")

    def test_one_product_per_top_level_folder_plus_root(self, folder_result):
        contracts, _ = folder_result
        ids = sorted(c["id"] for c in contracts)
        assert ids == ["gold.shopco.marketing", "gold.shopco.root", "gold.shopco.sales"]

    def test_models_land_in_their_folder_product(self, folder_result):
        by_id = _by_id(folder_result[0])
        sales = {e["exposeId"] for e in by_id["gold.shopco.sales"]["exposes"]}
        marketing = {e["exposeId"] for e in by_id["gold.shopco.marketing"]["exposes"]}
        assert sales == {"stg_orders", "orders_mart"}
        assert marketing == {"campaigns_mart"}
        assert {e["exposeId"] for e in by_id["gold.shopco.root"]["exposes"]} == {"overview"}

    def test_cross_folder_ref_becomes_cross_product_consumes(self, folder_result):
        by_id = _by_id(folder_result[0])
        marketing_consumes = {
            (c["productId"], c["exposeId"]) for c in by_id["gold.shopco.marketing"]["consumes"]
        }
        assert ("gold.shopco.sales", "orders_mart") in marketing_consumes
        root_consumes = {
            (c["productId"], c["exposeId"]) for c in by_id["gold.shopco.root"]["consumes"]
        }
        assert ("gold.shopco.sales", "orders_mart") in root_consumes
        assert ("gold.shopco.marketing", "campaigns_mart") in root_consumes

    def test_sources_stay_with_the_referencing_product(self, folder_result):
        by_id = _by_id(folder_result[0])
        sales_sources = {
            c["productId"]
            for c in by_id["gold.shopco.sales"]["consumes"]
            if "source" in c["productId"]
        }
        marketing_sources = {
            c["productId"]
            for c in by_id["gold.shopco.marketing"]["consumes"]
            if "source" in c["productId"]
        }
        assert sales_sources == {"source.raw"}
        assert marketing_sources == {"source.raw"}
        # sales consumes raw.events (with freshness), marketing raw.ad_spend
        sales_exposes = {c["exposeId"] for c in by_id["gold.shopco.sales"]["consumes"]}
        assert "events" in sales_exposes and "ad_spend" not in sales_exposes

    def test_every_split_contract_passes_schema_validation(self, folder_result, schema_manager):
        for contract in folder_result[0]:
            result = schema_manager.validate_contract(contract, offline_only=True)
            assert result.is_valid, f"{contract['id']}: {result.errors}"

    def test_build_model_root_scoped_to_folder(self, folder_result):
        by_id = _by_id(folder_result[0])
        assert by_id["gold.shopco.sales"]["builds"][0]["properties"]["model"] == "models/sales/"
        assert by_id["gold.shopco.root"]["builds"][0]["properties"]["model"] == "models/"

    def test_semantics_attach_once_to_the_owning_product(self, folder_result):
        by_id = _by_id(folder_result[0])
        assert "semantics" in _expose(by_id["gold.shopco.sales"], "orders_mart")
        for pid in ("gold.shopco.marketing", "gold.shopco.root"):
            assert all("semantics" not in e for e in by_id[pid]["exposes"])
        _, report = folder_result
        assert not any("semantic model" in x and "dropped" in x for x in report.unsupported)

    def test_report_entries_carry_product_prefixes(self, folder_result):
        _, report = folder_result
        assert any(x.startswith("[gold.shopco.sales] ") for x in report.mapped_one_to_one)
        assert any("split-by folder: 3 products" in x for x in report.notes)


class TestSplitByGroup:
    def test_one_product_per_group_with_ungrouped_bucket(self):
        contracts, report = _split_import("group")
        ids = sorted(c["id"] for c in contracts)
        assert ids == ["gold.shopco.marketing", "gold.shopco.sales", "gold.shopco.ungrouped"]
        assert any("ungrouped" in x and "overview" in x for x in report.notes)

    def test_groupless_manifest_fails_with_folder_suggestion(self):
        with pytest.raises(Exception, match="no dbt groups.*--split-by folder"):
            DbtManifestImporter().import_to_contracts(
                str(FIXTURES / "manifest_v12.json"), options={"split_by": "group"}
            )

    def test_group_split_contracts_pass_schema_validation(self, schema_manager):
        contracts, _ = _split_import("group")
        for contract in contracts:
            result = schema_manager.validate_contract(contract, offline_only=True)
            assert result.is_valid, f"{contract['id']}: {result.errors}"


class TestSplitDefaultStability:
    def test_project_mode_is_the_default_and_single(self):
        contracts, _ = _split_import("project")
        assert len(contracts) == 1
        assert contracts[0]["id"] == "gold.shopco"

    def test_singular_api_ignores_split_and_matches_project_mode(self):
        single, _ = DbtManifestImporter().import_to_contract(
            str(FIXTURES / MULTIDOMAIN), options={"split_by": "folder"}
        )
        project, _ = _split_import("project")
        assert single == project[0]

    def test_unknown_split_mode_rejected(self):
        with pytest.raises(ValueError, match="unknown split-by mode"):
            _split_import("banana")


class TestDbt110ArgumentsNesting:
    def test_arguments_nested_test_params_recovered(self):
        """dbt >=1.10 authors test params under `arguments:` — the reverse
        path must accept both that and the legacy flat kwargs shape."""
        contracts, _ = _split_import("project")
        orders = _expose(contracts[0], "orders_mart")
        rules = orders["contract"]["dq"]["rules"]
        vv = next(r for r in rules if r["type"] == "valid_values")
        assert vv["selector"] == "status"
        assert "paid" in vv.get("description", "") and "refunded" in vv.get("description", "")


class TestExposeDescriptionScrubbed:
    def test_hostile_jinja_in_model_description_is_stripped(self, tmp_path: Path):
        """The expose description round-trips into generated schema.yml (Jinja-
        rendered by dbt parse) — hostile templates must not survive import."""
        manifest = json.loads((FIXTURES / MULTIDOMAIN).read_text())
        node = manifest["nodes"]["model.shopco.orders_mart"]
        node["description"] = "Orders {{ env_var('AWS_SECRET_ACCESS_KEY') }} table"
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest))

        contract, report = DbtManifestImporter().import_to_contract(str(path))
        desc = _expose(contract, "orders_mart").get("description", "")
        assert "env_var" not in desc and "{{" not in desc
        assert "Orders" in desc and "table" in desc
        assert any("orders_mart description" in x for x in report.notes + report.unsupported)
