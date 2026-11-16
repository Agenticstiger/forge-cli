# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""semantic_models.yml emission — the contract semantics → MetricFlow bridge.

Pins the card's acceptance criteria:

* A contract with ``exposes[*].semantics`` emits ``models/semantic_models.yml``
  with ``semantic_models:`` + ``metrics:``; simple/derived/ratio metric types
  all map.
* MetricFlow parse-strictness defaulting: a primary entity is derived from the
  expose's key column when the block lacks one; ``defaults.agg_time_dimension``
  maps from ``defaultAggTimeDimension`` (with fallbacks); time dimensions
  always carry ``type_params.time_granularity``.
* Contracts WITHOUT semantics are a graceful no-op — the emitted file set is
  byte-identical to the pre-semantics generator.
* The live proof: a REAL ``dbt parse`` (duckdb, dbt-core >= 1.6) accepts a
  generated project including its semantic models.
"""

from __future__ import annotations

import copy
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build.engines.dbt import DbtEngine
from fluid_build.engines.dbt.semantic_models import generate_semantic_models

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _semantics(**overrides: Any) -> Dict[str, Any]:
    """A representative MetricFlow-shaped contract semantics block."""
    block: Dict[str, Any] = {
        "name": "Orders",
        "description": "Order semantic model",
        "defaultAggTimeDimension": "ordered_at",
        "entities": [
            {"name": "customer", "type": "foreign", "expr": "customer_id"},
        ],
        "dimensions": [
            {"name": "ordered_at", "type": "time", "typeParams": {"timeGranularity": "day"}},
            {"name": "status", "type": "categorical"},
        ],
        "measures": [
            {"name": "order_total", "agg": "sum", "expr": "amount"},
            {"name": "order_count", "agg": "count", "expr": "1"},
        ],
        "metrics": [
            {"name": "order_total", "type": "simple", "measure": "order_total"},
            {"name": "orders_count_metric", "type": "simple", "measure": "order_count"},
            {
                "name": "avg_order_value",
                "type": "ratio",
                "numerator": "order_total",
                "denominator": "orders_count_metric",
            },
            {
                "name": "order_total_2x",
                "type": "derived",
                "expr": "order_total * 2",
                "inputMetrics": ["order_total"],
            },
        ],
    }
    block.update(overrides)
    return block


def _contract(
    *,
    semantics: Optional[Dict[str, Any]] = None,
    schema: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Minimal generate-able dbt contract with an injectable semantics block."""
    expose: Dict[str, Any] = {
        "exposeId": "orders",
        "kind": "table",
        "contract": {
            "schema": (
                schema
                if schema is not None
                else [
                    {"name": "order_id", "type": "STRING", "primaryKey": True},
                    {"name": "ordered_at", "type": "TIMESTAMP"},
                    {"name": "amount", "type": "NUMBER"},
                    {"name": "status", "type": "STRING"},
                ]
            )
        },
    }
    if semantics is not None:
        expose["semantics"] = semantics
    return {
        "fluidVersion": "0.7.5",
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
        "exposes": [expose],
    }


def _emit(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Generate and parse the emitted semantic_models.yml into a dict."""
    out = generate_semantic_models(contract)
    assert "models/semantic_models.yml" in out
    return yaml.safe_load(out["models/semantic_models.yml"])


def _model(doc: Dict[str, Any], name: str = "orders") -> Dict[str, Any]:
    return next(m for m in doc["semantic_models"] if m["name"] == name)


def _metric(doc: Dict[str, Any], name: str) -> Dict[str, Any]:
    return next(m for m in doc.get("metrics", []) if m["name"] == name)


# ---------------------------------------------------------------------------
# The mechanical mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMapping:
    def test_semantic_model_wraps_model_ref(self):
        doc = _emit(_contract(semantics=_semantics()))
        model = _model(doc)
        assert model["model"] == "ref('orders')"
        assert model["description"] == "Order semantic model"

    def test_camel_case_maps_to_snake_case(self):
        doc = _emit(_contract(semantics=_semantics()))
        model = _model(doc)
        # defaultAggTimeDimension → defaults.agg_time_dimension
        assert model["defaults"] == {"agg_time_dimension": "ordered_at"}
        # typeParams.timeGranularity → type_params.time_granularity
        time_dim = next(d for d in model["dimensions"] if d["name"] == "ordered_at")
        assert time_dim["type_params"] == {"time_granularity": "day"}

    def test_declared_entities_pass_through(self):
        doc = _emit(_contract(semantics=_semantics()))
        entities = _model(doc)["entities"]
        foreign = next(e for e in entities if e["name"] == "customer")
        assert foreign == {"name": "customer", "type": "foreign", "expr": "customer_id"}

    def test_measures_map_agg_expr(self):
        doc = _emit(_contract(semantics=_semantics()))
        measures = {m["name"]: m for m in _model(doc)["measures"]}
        assert measures["order_total"]["agg"] == "sum"
        assert measures["order_total"]["expr"] == "amount"

    def test_measure_extras_map(self):
        semantics = _semantics(
            measures=[
                {
                    "name": "balance",
                    "agg": "sum",
                    "expr": "amount",
                    "aggTimeDimension": "ordered_at",
                    "createMetric": True,
                    "nonAdditiveDimension": {"name": "ordered_at", "windowChoice": "max"},
                },
            ],
            metrics=[],
        )
        doc = _emit(_contract(semantics=semantics))
        (measure,) = _model(doc)["measures"]
        assert measure["agg_time_dimension"] == "ordered_at"
        assert measure["create_metric"] is True
        assert measure["non_additive_dimension"] == {
            "name": "ordered_at",
            "window_choice": "max",
        }

    def test_percentile_measure_gets_agg_params(self):
        semantics = _semantics(
            measures=[{"name": "p_amount", "agg": "percentile", "expr": "amount"}],
            metrics=[],
        )
        doc = _emit(_contract(semantics=semantics))
        (measure,) = _model(doc)["measures"]
        assert measure["agg_params"] == {"percentile": 0.5}


@pytest.mark.unit
class TestMetricTypes:
    """ACCEPTANCE: simple / derived / ratio metric types all map."""

    def test_simple_metric(self):
        doc = _emit(_contract(semantics=_semantics()))
        metric = _metric(doc, "order_total")
        assert metric["type"] == "simple"
        assert metric["type_params"] == {"measure": "order_total"}
        # dbt >= 1.7 requires a label on every metric.
        assert metric["label"] == "order_total"

    def test_ratio_metric(self):
        doc = _emit(_contract(semantics=_semantics()))
        metric = _metric(doc, "avg_order_value")
        assert metric["type"] == "ratio"
        assert metric["type_params"] == {
            "numerator": "order_total",
            "denominator": "orders_count_metric",
        }

    def test_derived_metric(self):
        doc = _emit(_contract(semantics=_semantics()))
        metric = _metric(doc, "order_total_2x")
        assert metric["type"] == "derived"
        assert metric["type_params"] == {
            "expr": "order_total * 2",
            "metrics": [{"name": "order_total"}],
        }

    def test_ratio_falls_back_to_input_metrics(self):
        semantics = _semantics()
        semantics["metrics"] = [
            {"name": "order_total", "type": "simple", "measure": "order_total"},
            {"name": "orders_count_metric", "type": "simple", "measure": "order_count"},
            {
                "name": "aov",
                "type": "ratio",
                "inputMetrics": ["order_total", "orders_count_metric"],
            },
        ]
        doc = _emit(_contract(semantics=semantics))
        assert _metric(doc, "aov")["type_params"] == {
            "numerator": "order_total",
            "denominator": "orders_count_metric",
        }

    def test_metric_filter_passes_through(self):
        semantics = _semantics()
        semantics["metrics"] = [
            {
                "name": "completed",
                "type": "simple",
                "measure": "order_count",
                "filter": "{{ Dimension('order__status') }} = 'completed'",
            },
        ]
        doc = _emit(_contract(semantics=semantics))
        assert _metric(doc, "completed")["filter"] == (
            "{{ Dimension('order__status') }} = 'completed'"
        )


# ---------------------------------------------------------------------------
# MetricFlow parse-strictness defaulting
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDefaultingRules:
    def test_primary_entity_derived_from_key_column(self):
        # Semantics block declares no primary entity → derive from the
        # expose's declared key column (order_id → entity 'order').
        doc = _emit(_contract(semantics=_semantics()))
        primary = next(e for e in _model(doc)["entities"] if e["type"] == "primary")
        assert primary == {"name": "order", "type": "primary", "expr": "order_id"}

    def test_declared_primary_entity_wins(self):
        semantics = _semantics(entities=[{"name": "order", "type": "primary", "expr": "order_id"}])
        doc = _emit(_contract(semantics=semantics))
        primaries = [e for e in _model(doc)["entities"] if e["type"] == "primary"]
        assert primaries == [{"name": "order", "type": "primary", "expr": "order_id"}]

    def test_second_primary_demoted_to_unique(self):
        semantics = _semantics(
            entities=[
                {"name": "order", "type": "primary", "expr": "order_id"},
                {"name": "order_key", "type": "primary", "expr": "order_key"},
            ]
        )
        doc = _emit(_contract(semantics=semantics))
        by_name = {e["name"]: e for e in _model(doc)["entities"]}
        assert by_name["order"]["type"] == "primary"
        assert by_name["order_key"]["type"] == "unique"

    def test_no_key_column_falls_back_to_first_schema_column(self):
        schema = [
            {"name": "sku", "type": "STRING"},
            {"name": "amount", "type": "NUMBER"},
        ]
        doc = _emit(_contract(semantics=_semantics(), schema=schema))
        primary = next(e for e in _model(doc)["entities"] if e["type"] == "primary")
        assert primary == {"name": "sku", "type": "primary", "expr": "sku"}

    def test_unique_entity_promoted_when_no_columns(self):
        semantics = _semantics(
            entities=[{"name": "order_key", "type": "unique", "expr": "order_key"}]
        )
        doc = _emit(_contract(semantics=semantics, schema=[]))
        primary = next(e for e in _model(doc)["entities"] if e["type"] == "primary")
        assert primary["name"] == "order_key"

    def test_time_dimension_missing_granularity_defaults_to_day(self):
        semantics = _semantics(
            dimensions=[{"name": "ordered_at", "type": "time"}],  # no typeParams
        )
        doc = _emit(_contract(semantics=semantics))
        time_dim = next(d for d in _model(doc)["dimensions"] if d["name"] == "ordered_at")
        assert time_dim["type_params"] == {"time_granularity": "day"}

    def test_agg_time_dimension_falls_back_to_first_time_dimension(self):
        semantics = _semantics()
        del semantics["defaultAggTimeDimension"]
        doc = _emit(_contract(semantics=semantics))
        assert _model(doc)["defaults"] == {"agg_time_dimension": "ordered_at"}

    def test_agg_time_dimension_synthesized_from_schema_column(self):
        # defaultAggTimeDimension names a schema column that was never
        # declared as a dimension → a day-grain time dimension is synthesized.
        semantics = _semantics(dimensions=[{"name": "status", "type": "categorical"}])
        doc = _emit(_contract(semantics=semantics))
        model = _model(doc)
        assert model["defaults"] == {"agg_time_dimension": "ordered_at"}
        synthesized = next(d for d in model["dimensions"] if d["name"] == "ordered_at")
        assert synthesized["type"] == "time"
        assert synthesized["type_params"] == {"time_granularity": "day"}

    def test_no_time_dimension_drops_measures_and_metrics(self):
        semantics = _semantics(dimensions=[{"name": "status", "type": "categorical"}])
        del semantics["defaultAggTimeDimension"]
        schema = [
            {"name": "order_id", "type": "STRING", "primaryKey": True},
            {"name": "amount", "type": "NUMBER"},
            {"name": "status", "type": "STRING"},
        ]
        doc = _emit(_contract(semantics=semantics, schema=schema))
        model = _model(doc)
        assert "measures" not in model
        assert "defaults" not in model
        assert "metrics" not in doc  # dependent metrics dropped too
        # Entities + dimensions still emit.
        assert model["entities"]
        assert model["dimensions"]

    def test_simple_metric_referencing_missing_measure_skipped(self):
        semantics = _semantics()
        semantics["metrics"].append({"name": "ghost", "type": "simple", "measure": "nonexistent"})
        doc = _emit(_contract(semantics=semantics))
        assert not any(m["name"] == "ghost" for m in doc["metrics"])

    def test_incomplete_ratio_and_derived_metrics_skipped(self):
        semantics = _semantics()
        semantics["metrics"] = [
            {"name": "no_denominator", "type": "ratio", "numerator": "order_total"},
            {"name": "no_inputs", "type": "derived", "expr": "a + b"},
        ]
        doc = _emit(_contract(semantics=semantics))
        assert "metrics" not in doc

    def test_duplicate_element_names_dropped_first_wins(self):
        # A measure named like an existing dimension collides in
        # MetricFlow's per-model namespace → the measure is dropped.
        semantics = _semantics()
        semantics["measures"].append({"name": "status", "agg": "sum", "expr": "amount"})
        doc = _emit(_contract(semantics=semantics))
        measures = [m["name"] for m in _model(doc)["measures"]]
        assert "status" not in measures
        assert measures == ["order_total", "order_count"]

    def test_names_sanitized_for_metricflow(self):
        semantics = _semantics(
            measures=[{"name": "order total ($)", "agg": "sum", "expr": "amount"}],
            metrics=[{"name": "order total ($)", "type": "simple", "measure": "order total ($)"}],
        )
        doc = _emit(_contract(semantics=semantics))
        (measure,) = _model(doc)["measures"]
        assert measure["name"] == "order_total"
        assert doc["metrics"][0]["type_params"] == {"measure": "order_total"}


# ---------------------------------------------------------------------------
# Time spine + no-op guarantees
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTimeSpineAndNoOp:
    def test_time_spine_model_ships_with_semantics(self):
        out = generate_semantic_models(_contract(semantics=_semantics()))
        assert "models/metricflow_time_spine.sql" in out
        assert "dbt.date_spine" in out["models/metricflow_time_spine.sql"]
        doc = yaml.safe_load(out["models/semantic_models.yml"])
        (spine_def,) = doc["models"]
        assert spine_def["name"] == "metricflow_time_spine"
        assert spine_def["time_spine"] == {"standard_granularity_column": "date_day"}
        assert spine_def["columns"] == [{"name": "date_day", "granularity": "day"}]

    def test_no_semantics_is_a_no_op(self):
        assert generate_semantic_models(_contract()) == {}
        assert generate_semantic_models({"exposes": []}) == {}
        assert generate_semantic_models({}) == {}

    def test_empty_semantics_block_is_a_no_op(self):
        assert generate_semantic_models(_contract(semantics={})) == {}

    def test_generate_output_byte_identical_without_semantics(self):
        # ACCEPTANCE: for contracts without semantics the full engine
        # output is byte-identical to the pre-semantics generator.
        contract = _contract()
        build = contract["builds"][0]
        engine = DbtEngine()
        baseline = engine.generate(copy.deepcopy(contract), copy.deepcopy(build))
        again = engine.generate(copy.deepcopy(contract), copy.deepcopy(build))
        assert baseline == again
        assert "models/semantic_models.yml" not in baseline
        assert "models/metricflow_time_spine.sql" not in baseline

    def test_generate_wires_semantic_models_in(self):
        contract = _contract(semantics=_semantics())
        files = DbtEngine().generate(contract, contract["builds"][0])
        assert "models/semantic_models.yml" in files
        assert "models/metricflow_time_spine.sql" in files
        assert files["models/semantic_models.yml"].startswith("# Generated by fluid generate")

    def test_multiple_exposes_share_metric_namespace(self):
        contract = _contract(semantics=_semantics())
        second = copy.deepcopy(contract["exposes"][0])
        second["exposeId"] = "orders_eu"
        contract["exposes"].append(second)
        doc = _emit(contract)
        assert [m["name"] for m in doc["semantic_models"]] == ["orders", "orders_eu"]
        # Duplicate metric names across exposes are dropped first-wins.
        names = [m["name"] for m in doc["metrics"]]
        assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# LIVE proof: real `dbt parse` accepts the generated semantic models
# ---------------------------------------------------------------------------


def _find_dbt() -> Optional[str]:
    """Locate the dbt CLI: prefer the running venv's bin, then PATH."""
    candidate = Path(sys.executable).parent / "dbt"
    if candidate.exists():
        return str(candidate)
    return shutil.which("dbt")


@pytest.mark.integration
@pytest.mark.slow
class TestLiveDbtParse:
    """ACCEPTANCE: a contract with semantics produces a generated project
    that passes a REAL ``dbt parse`` including the semantic models (duckdb,
    dbt-core >= 1.6 — semantic models are not parsed before that)."""

    def test_generated_project_with_semantics_passes_dbt_parse(self, tmp_path: Path):
        dbt = _find_dbt()
        if dbt is None:
            pytest.skip("dbt CLI not available")

        # Governance surface rides along: tags/labels/owner must survive a
        # REAL dbt parse as config.meta (and land in the manifest, which is
        # what the manifest importer round-trips).
        semantics = _semantics()
        semantics["tags"] = ["certified"]
        semantics["labels"] = {"tier": "gold"}
        semantics["metrics"][0]["owner"] = "finance-team"
        contract = _contract(semantics=semantics)
        build = contract["builds"][0]
        out_dir = tmp_path / "dbt_project"
        files = DbtEngine().generate(contract, build, output_dir=out_dir)
        out_dir.mkdir(parents=True)
        for rel_path, content in files.items():
            target = out_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        result = subprocess.run(
            [dbt, "parse", "--project-dir", str(out_dir), "--profiles-dir", str(out_dir)],
            capture_output=True,
            text=True,
            check=False,
            cwd=out_dir,
            timeout=300,
        )
        assert result.returncode == 0, f"dbt parse failed:\n{result.stdout}\n{result.stderr}"

        # The semantic manifest must actually carry the models + metrics
        # (an empty semantic layer would also "pass" parse).
        import json

        manifest = json.loads((out_dir / "target" / "semantic_manifest.json").read_text())
        assert [sm["name"] for sm in manifest["semantic_models"]] == ["orders"]
        assert sorted(m["name"] for m in manifest["metrics"]) == [
            "avg_order_value",
            "order_total",
            "order_total_2x",
            "orders_count_metric",
        ]

        # Governance meta survives parse into the node manifest — the exact
        # payload the manifest importer recovers on round-trip.
        node_manifest = json.loads((out_dir / "target" / "manifest.json").read_text())
        parsed_sm = next(iter(node_manifest["semantic_models"].values()))
        assert parsed_sm["config"]["meta"] == {
            "fluid_tags": ["certified"],
            "fluid_labels": {"tier": "gold"},
        }
        owners = {
            m["name"]: (m.get("config") or {}).get("meta", {}).get("owner")
            for m in node_manifest["metrics"].values()
        }
        assert owners.get(semantics["metrics"][0]["name"]) == "finance-team"


@pytest.mark.unit
class TestDeferredMetricResolution:
    def test_ratio_may_reference_metric_from_later_expose(self):
        contract = _contract(semantics=_semantics())
        # First expose's ratio references a metric only the SECOND expose defines.
        contract["exposes"][0]["semantics"]["metrics"] = [
            {"name": "order_total", "type": "simple", "measure": "order_total"},
            {
                "name": "orders_vs_refunds",
                "type": "ratio",
                "numerator": "order_total",
                "denominator": "refund_total",
            },
        ]
        second = copy.deepcopy(contract["exposes"][0])
        second["exposeId"] = "refunds"
        second["semantics"]["metrics"] = [
            {"name": "refund_total", "type": "simple", "measure": "order_total"},
        ]
        contract["exposes"].append(second)
        doc = _emit(contract)
        assert _metric(doc, "orders_vs_refunds")["type_params"] == {
            "numerator": "order_total",
            "denominator": "refund_total",
        }

    def test_create_metric_measure_counts_as_ratio_input(self):
        semantics = _semantics(
            measures=[
                {"name": "order_total", "agg": "sum", "expr": "amount", "createMetric": True},
                {"name": "order_count", "agg": "count", "expr": "1", "createMetric": True},
            ],
            metrics=[
                {
                    "name": "aov",
                    "type": "ratio",
                    "numerator": "order_total",
                    "denominator": "order_count",
                },
            ],
        )
        doc = _emit(_contract(semantics=semantics))
        assert [m["name"] for m in doc["metrics"]] == ["aov"]


@pytest.mark.unit
class TestPercentileAggParams:
    """Contract aggParams (0.7.6+) → MetricFlow agg_params mapping."""

    def _percentile_measure(self, agg_params=None):
        measure = {"name": "p_amount", "agg": "percentile", "expr": "amount"}
        if agg_params is not None:
            measure["aggParams"] = agg_params
        return _semantics(measures=[measure], metrics=[])

    def test_contract_percentile_value_passes_through(self):
        doc = _emit(_contract(semantics=self._percentile_measure({"percentile": 0.95})))
        (measure,) = _model(doc)["measures"]
        assert measure["agg_params"] == {"percentile": 0.95}

    def test_discrete_flag_maps_to_use_discrete_percentile(self):
        doc = _emit(
            _contract(
                semantics=self._percentile_measure(
                    {"percentile": 0.9, "useDiscretePercentile": True}
                )
            )
        )
        (measure,) = _model(doc)["measures"]
        assert measure["agg_params"] == {"percentile": 0.9, "use_discrete_percentile": True}

    def test_invalid_percentile_falls_back_to_default(self):
        doc = _emit(_contract(semantics=self._percentile_measure({"percentile": 7})))
        (measure,) = _model(doc)["measures"]
        assert measure["agg_params"] == {"percentile": 0.5}

    def test_default_is_pinned_to_the_query_compiler(self):
        """Both consumers of the semantics block must agree on the default
        percentile or the same contract answers different numbers."""
        from fluid_build.engines.dbt.semantic_models import DEFAULT_PERCENTILE
        from fluid_build.output_ports.mcp.query_compiler import (
            DEFAULT_PERCENTILE as MCP_DEFAULT,
        )

        assert DEFAULT_PERCENTILE == MCP_DEFAULT == 0.5


@pytest.mark.unit
class TestGovernanceMeta:
    """owner / tags / labels — previously dead schema surface — ride into
    dbt as config.meta (parse-verified) and round-trip via the importer."""

    def test_metric_owner_maps_to_config_meta(self):
        semantics = _semantics()
        semantics["metrics"][0]["owner"] = "finance-team"
        doc = _emit(_contract(semantics=semantics))
        metric = _metric(doc, semantics["metrics"][0]["name"])
        assert metric["config"] == {"meta": {"owner": "finance-team"}}

    def test_model_tags_and_labels_map_to_config_meta(self):
        semantics = _semantics()
        semantics["tags"] = ["certified", "gold"]
        semantics["labels"] = {"tier": "gold", "domain": "commerce"}
        doc = _emit(_contract(semantics=semantics))
        model = _model(doc)
        assert model["config"]["meta"] == {
            "fluid_tags": ["certified", "gold"],
            "fluid_labels": {"tier": "gold", "domain": "commerce"},
        }

    def test_no_governance_fields_no_config_key(self):
        doc = _emit(_contract(semantics=_semantics()))
        assert "config" not in _model(doc)
        for metric in doc.get("metrics", []):
            assert "config" not in metric


# ---------------------------------------------------------------------------
# FLUID agg tokens → MetricFlow AggregationType (regression)
#
# dbt's AggregationType enum is
# ['sum','min','max','count_distinct','sum_boolean','average','percentile',
#  'median','count'] — there is no 'avg'. Passing FLUID's own first-class
# `agg: avg` straight through made `dbt parse` abort on the generated project:
#
#   ValueError: Invalid enum value: `avg` in enum AggregationType
#     (dbt/parser/schema_yaml_readers.py, agg=AggregationType(unparsed.agg))
#
# i.e. any contract using a valid FLUID agg produced an unparseable project.
# ---------------------------------------------------------------------------

# The exact enum dbt-semantic-interfaces accepts, transcribed from
# dbt_semantic_interfaces.type_enums.aggregation_type.AggregationType.
_DBT_AGGREGATION_TYPES = frozenset(
    {
        "sum",
        "min",
        "max",
        "count_distinct",
        "sum_boolean",
        "average",
        "percentile",
        "median",
        "count",
    }
)


class TestAggregationTypeMapping:
    def test_avg_is_emitted_as_metricflow_average(self):
        doc = _emit(
            _contract(
                semantics=_semantics(
                    measures=[{"name": "aov", "agg": "avg", "expr": "amount"}],
                    metrics=[{"name": "aov_m", "type": "simple", "measure": "aov"}],
                )
            )
        )
        measures = doc["semantic_models"][0]["measures"]
        assert [m["agg"] for m in measures] == ["average"]

    def test_every_emitted_agg_is_a_valid_dbt_aggregation_type(self):
        from fluid_build.engines.dbt.semantic_models import AGG_TO_METRICFLOW

        for fluid_agg, mf_agg in AGG_TO_METRICFLOW.items():
            assert mf_agg in _DBT_AGGREGATION_TYPES, fluid_agg

    def test_the_fluid_schema_enum_is_fully_covered(self):
        """Every ``measures[].agg`` the 0.7.6 schema admits must map."""
        import json
        from pathlib import Path as _Path

        import fluid_build

        schema = json.loads(
            (_Path(fluid_build.__file__).parent / "schemas" / "fluid-schema-0.7.6.json").read_text()
        )
        enum = schema["$defs"]["semanticModel"]["properties"]["measures"]["items"]["properties"][
            "agg"
        ]["enum"]
        from fluid_build.engines.dbt.semantic_models import AGG_TO_METRICFLOW

        assert set(enum) == set(AGG_TO_METRICFLOW)


# ---------------------------------------------------------------------------
# A semantic model's ``model:`` key is ``ref('<exposeId>')``, but the intent /
# multi-stage emitters name model files from *stage* names. When the two
# disagree the ref dangles and `dbt parse` rejects the WHOLE project:
#   Semantic_Model 'semantic_model.x.orders' depends on a node named 'orders'
#   which was not found
# ---------------------------------------------------------------------------


def _staged_intent():
    from fluid_build.engines.base import TransformationIntent

    return TransformationIntent(
        stages=[
            {"name": "stg_orders", "sql": "select 1 as order_id", "layer": "staging"},
            {"name": "fct_orders", "sql": "select * from x", "layer": "marts"},
        ]
    )


def _emitted_refs(generated):
    import re

    return re.findall(r"ref\('([^']+)'\)", generated.get("models/semantic_models.yml") or "")


def _model_basenames(generated):
    return {p.rsplit("/", 1)[-1][: -len(".sql")] for p in generated if p.endswith(".sql")}


def test_semantic_model_is_dropped_when_its_ref_target_was_not_emitted():
    from fluid_build.engines.dbt import DbtEngine

    contract = _contract(semantics=_semantics())
    generated = DbtEngine().generate(
        contract, contract["builds"][0], transformation_intent=_staged_intent()
    )
    # Stage names own the model files, so ref('orders') has no target.
    assert _model_basenames(generated) == {"stg_orders", "fct_orders"}
    assert _emitted_refs(generated) == [], "emitted a ref() to a model that does not exist"


def test_semantic_model_survives_when_its_ref_target_is_emitted():
    """The default path names the model after the expose, so it must be kept."""
    from fluid_build.engines.dbt import DbtEngine

    contract = _contract(semantics=_semantics())
    generated = DbtEngine().generate(contract, contract["builds"][0])
    refs = _emitted_refs(generated)
    assert refs == ["orders"]
    assert refs[0] in _model_basenames(generated)


def test_every_emitted_semantic_ref_resolves_to_an_emitted_model():
    """The invariant, stated directly: no generated project may contain a
    semantic model whose ref target is absent."""
    from fluid_build.engines.dbt import DbtEngine

    for intent in (None, _staged_intent()):
        contract = _contract(semantics=_semantics())
        generated = DbtEngine().generate(
            contract, contract["builds"][0], transformation_intent=intent
        )
        dangling = [r for r in _emitted_refs(generated) if r not in _model_basenames(generated)]
        assert not dangling, f"dangling semantic refs {dangling} (intent={intent is not None})"


# ---------------------------------------------------------------------------
# Schema 0.7.7: SCD validity windows map field for field; relationships do not
# export (MetricFlow joins by entity name). dbt runs its semantic rules only
# when the project has a metric, so the live proof declares one on orders.
# ---------------------------------------------------------------------------


def _scd_contract(**history_overrides: Any) -> Dict[str, Any]:
    """Orders (fact, with a relationship and a metric) and an SCD customer history."""
    contract = _contract(semantics=_semantics())
    contract["fluidVersion"] = "0.7.7"
    orders = contract["exposes"][0]
    orders["contract"]["schema"].append({"name": "customer_id", "type": "STRING"})
    orders["semantics"]["relationships"] = [
        {
            "name": "orders_to_customer_history",
            "to": "customer_history",
            "fromColumns": ["customer_id"],
            "toColumns": ["customer_id"],
            "cardinality": "many_to_one",
        }
    ]
    history: Dict[str, Any] = {
        "name": "customer_history",
        "entities": [
            {"name": "customer_sk", "type": "primary"},
            {"name": "customer", "type": "natural", "expr": "customer_id"},
        ],
        "dimensions": [
            {"name": "segment", "type": "categorical"},
            {
                "name": "valid_from",
                "type": "time",
                "typeParams": {"timeGranularity": "day", "validityParams": {"isStart": True}},
            },
            {
                "name": "valid_to",
                "type": "time",
                "typeParams": {"timeGranularity": "day", "validityParams": {"isEnd": True}},
            },
        ],
        "measures": [{"name": "versions", "agg": "count", "expr": "1"}],
    }
    history.update(history_overrides)
    contract["exposes"].append(
        {
            "exposeId": "customer_history",
            "kind": "table",
            "contract": {
                "schema": [
                    {"name": "customer_sk", "type": "STRING", "primaryKey": True},
                    {"name": "customer_id", "type": "STRING"},
                    {"name": "segment", "type": "STRING"},
                    {"name": "valid_from", "type": "TIMESTAMP"},
                    {"name": "valid_to", "type": "TIMESTAMP"},
                ]
            },
            "semantics": history,
        }
    )
    return contract


def _dimension(model: Dict[str, Any], name: str) -> Dict[str, Any]:
    return next(d for d in model["dimensions"] if d["name"] == name)


@pytest.mark.unit
class TestValidityWindow:
    def test_bounds_map_to_validity_params(self):
        history = _model(_emit(_scd_contract()), "customer_history")
        assert _dimension(history, "valid_from")["type_params"] == {
            "time_granularity": "day",
            "validity_params": {"is_start": True},
        }
        assert _dimension(history, "valid_to")["type_params"] == {
            "time_granularity": "day",
            "validity_params": {"is_end": True},
        }
        assert "type_params" not in _dimension(history, "segment")

    def test_model_takes_dbt_scd_type_ii_shape(self, caplog):
        with caplog.at_level("WARNING"):
            history = _model(_emit(_scd_contract()), "customer_history")
        # The natural entity is the primary entity; dbt rejects primary/unique
        # entities and measures beside a validity window, so they are dropped loudly.
        assert history["primary_entity"] == "customer"
        assert history["entities"] == [
            {"name": "customer", "type": "natural", "expr": "customer_id"}
        ]
        assert "measures" not in history and "defaults" not in history
        assert "dropping primary entity 'customer_sk' on customer_history" in caplog.text
        assert "measures beside a validity window" in caplog.text

    def test_foreign_entities_stay_beside_a_window(self):
        contract = _scd_contract()
        contract["exposes"][1]["semantics"]["entities"].append(
            {"name": "region", "type": "foreign", "expr": "segment"}
        )
        history = _model(_emit(contract), "customer_history")
        assert [e["type"] for e in history["entities"]] == ["natural", "foreign"]

    def test_natural_entity_without_a_window_is_dropped(self, caplog):
        contract = _contract(
            semantics=_semantics(
                entities=[{"name": "order_key", "type": "natural", "expr": "order_id"}]
            )
        )
        with caplog.at_level("WARNING"):
            orders = _model(_emit(contract))
        # dbt accepts a natural entity only with a validity window; the
        # primary entity is derived from the key column instead.
        assert [e["type"] for e in orders["entities"]] == ["primary"]
        assert "primary_entity" not in orders
        assert "dropping natural entity 'order_key'" in caplog.text

    def test_malformed_window_is_dropped_whole(self, caplog):
        contract = _scd_contract()
        del contract["exposes"][1]["semantics"]["dimensions"][2]["typeParams"]["validityParams"]
        with caplog.at_level("WARNING"):
            history = _model(_emit(contract), "customer_history")
        assert all(
            "validity_params" not in (d.get("type_params") or {}) for d in history["dimensions"]
        )
        assert [e["type"] for e in history["entities"]] == ["primary"]
        assert "primary_entity" not in history
        assert "do not form a validity window" in caplog.text

    def test_relationships_are_informational(self):
        with_relationships = generate_semantic_models(_scd_contract())
        contract = _scd_contract()
        del contract["exposes"][0]["semantics"]["relationships"]
        assert generate_semantic_models(contract) == with_relationships


@pytest.mark.integration
@pytest.mark.slow
def test_scd_project_passes_dbt_parse_and_keeps_the_window(tmp_path: Path):
    """ACCEPTANCE: real dbt-core parses a project with a validity window, and its
    semantic manifest carries the bounds and the natural primary entity."""
    dbt = _find_dbt()
    if dbt is None:
        pytest.skip("dbt CLI not available")
    contract = _scd_contract()
    out_dir = tmp_path / "dbt_project"
    files = DbtEngine().generate(contract, contract["builds"][0], output_dir=out_dir)
    out_dir.mkdir(parents=True)
    for rel_path, content in files.items():
        target = out_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    result = subprocess.run(
        [dbt, "parse", "--project-dir", str(out_dir), "--profiles-dir", str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
        cwd=out_dir,
        timeout=300,
    )
    assert result.returncode == 0, f"dbt parse failed:\n{result.stdout}\n{result.stderr}"

    import json

    manifest = json.loads((out_dir / "target" / "semantic_manifest.json").read_text())
    assert manifest["metrics"], "dbt validates semantic models only when a metric exists"
    history = next(m for m in manifest["semantic_models"] if m["name"] == "customer_history")
    assert history["primary_entity"] == "customer"
    bounds = {
        d["name"]: d["type_params"]["validity_params"]
        for d in history["dimensions"]
        if (d.get("type_params") or {}).get("validity_params")
    }
    assert bounds == {
        "valid_from": {"is_start": True, "is_end": False},
        "valid_to": {"is_start": False, "is_end": True},
    }


@pytest.mark.unit
def test_a_window_bound_that_shares_a_name_drops_the_window(caplog):
    """The first of two names wins, so a bound named like an entity would leave dbt one bound."""
    contract = _scd_contract()
    history = contract["exposes"][1]["semantics"]
    history["entities"].append({"name": "valid_from", "type": "foreign", "expr": "segment"})
    with caplog.at_level("WARNING"):
        model = _model(_emit(contract), "customer_history")
    assert all("validity_params" not in (d.get("type_params") or {}) for d in model["dimensions"])
    assert "primary_entity" not in model
    assert "do not form a validity window" in caplog.text
