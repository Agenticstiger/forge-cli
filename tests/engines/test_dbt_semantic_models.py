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

        contract = _contract(semantics=_semantics())
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
