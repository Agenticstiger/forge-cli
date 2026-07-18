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

"""Apache Ossie interchange-document conformance gate.

The ``*.semantics.osi.*`` sidecar exists purely for interchange — dbt
Core's native OSI reader, the upstream Snowflake Cortex / Salesforce /
GoodData converters. A sidecar that fails the upstream JSON Schema is
worthless, and historically nothing pinned that: fluid emitted a bare
semantic model (no ``{version, semantic_model: []}`` root wrapper) with
internal-IR-only fields (``data_type`` / ``grain`` / relationship
``description``) inline, all of which strict validators reject
(``additionalProperties: false`` everywhere upstream).

This file is the hard CI gate: every emitted document must validate
against the **vendored verbatim upstream schema**
(``fluid_build/schemas/ossie-osi-schema.json``, from apache/ossie
``core-spec/osi-schema.json``). The conformance agent runs the same
check at runtime as advisory warnings; here it is an assertion.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import yaml

from fluid_build.copilot.schemas.osi import (
    FLUID_VENDOR_NAME,
    OSI_SPEC_VERSION,
    OSIAIContext,
    OSIDataset,
    OSIDimension,
    OSIDocument,
    OSIExpression,
    OSIExpressionDialect,
    OSIField,
    OSIMetric,
    OSIRelationship,
    OSISemanticModel,
)
from fluid_build.copilot.schemas.stage_outputs import ConceptualDraft, LogicalDraft
from fluid_build.forge_datamodel.emit.osi_sidecar import (
    build_osi_document,
    emit_osi_json,
    emit_osi_yaml,
    validate_osi_document,
)


def _make_logical(*, technique: str = "flat") -> LogicalDraft:
    """Representative draft exercising every relocation path: data_type,
    time grain, relationship description, a field with no expression."""
    osi = OSISemanticModel(
        name="customer_orders",
        description="Customer order facts and dimensions",
        ai_context=OSIAIContext(
            instructions="Use for customer revenue analysis",
            synonyms=["customer purchases"],
        ),
        datasets=[
            OSIDataset(
                name="orders",
                source="raw.orders",
                primary_key=["order_id"],
                unique_keys=[["order_number"]],
                fields=[
                    OSIField(
                        name="order_date",
                        expression=OSIExpression(
                            dialects=[
                                OSIExpressionDialect(dialect="ANSI_SQL", expression="order_date"),
                                OSIExpressionDialect(
                                    dialect="BIGQUERY", expression="CAST(order_date AS DATE)"
                                ),
                            ]
                        ),
                        dimension=OSIDimension(is_time=True, grain="day"),
                        data_type="DATE",
                    ),
                    # No expression — the emitter must synthesize the
                    # spec-required bare column reference.
                    OSIField(name="customer_id", data_type="STRING"),
                    OSIField(name="amount", data_type="NUMBER"),
                ],
            ),
            OSIDataset(
                # No source — the emitter must default it to the name.
                name="customers",
                primary_key=["id"],
                fields=[OSIField(name="id")],
            ),
        ],
        relationships=[
            OSIRelationship(
                name="orders_to_customers",
                **{"from": "orders", "to": "customers"},
                from_columns=["customer_id"],
                to_columns=["id"],
                description="FK inferred from column naming",
            )
        ],
        metrics=[
            OSIMetric(
                name="total_revenue",
                description="Total revenue from all orders",
                expression=OSIExpression(
                    dialects=[
                        OSIExpressionDialect(dialect="ANSI_SQL", expression="SUM(orders.amount)")
                    ]
                ),
            )
        ],
    )
    return LogicalDraft(
        name="customer_orders",
        description="Flat draft for conformance testing",
        technique=technique,
        osi=osi,
        conceptual=ConceptualDraft(name="customer_orders"),
    )


class TestDocumentShape:
    def test_root_wrapper_and_version(self) -> None:
        doc = build_osi_document(_make_logical())
        assert set(doc.keys()) == {"version", "semantic_model"}
        assert doc["version"] == OSI_SPEC_VERSION
        assert isinstance(doc["semantic_model"], list) and len(doc["semantic_model"]) == 1

    def test_document_validates_against_vendored_upstream_schema(self) -> None:
        """The headline gate: zero issues against the verbatim upstream
        JSON Schema."""
        issues = validate_osi_document(build_osi_document(_make_logical()))
        assert issues == [], issues

    def test_yaml_and_json_emissions_round_trip_identically(self) -> None:
        logical = _make_logical()
        doc = build_osi_document(logical)
        assert yaml.safe_load(emit_osi_yaml(logical)) == doc
        assert json.loads(emit_osi_json(logical)) == doc

    def test_osi_document_model_defaults_to_released_spec_version(self) -> None:
        """OSI_SPEC_VERSION is the single chokepoint for the emitted
        version — 0.1.1 is the only released spec revision and the only
        one dbt Core's native OSI reader accepts today."""
        assert OSIDocument().version == OSI_SPEC_VERSION == "0.1.1"


class TestIRFieldRelocation:
    """Internal-IR-only fields must move into FLUID custom_extensions —
    never appear inline (additionalProperties: false upstream)."""

    def _fields_by_name(self, doc: dict) -> dict:
        dataset = doc["semantic_model"][0]["datasets"][0]
        return {f["name"]: f for f in dataset["fields"]}

    def _fluid_ext(self, node: dict) -> dict:
        payloads = [
            json.loads(ext["data"])
            for ext in node.get("custom_extensions", [])
            if ext["vendor_name"] == FLUID_VENDOR_NAME
        ]
        assert payloads, f"expected a FLUID custom_extension on {node.get('name')!r}"
        return payloads[0]

    def test_data_type_relocates(self) -> None:
        fields = self._fields_by_name(build_osi_document(_make_logical()))
        assert "data_type" not in fields["customer_id"]
        assert self._fluid_ext(fields["customer_id"])["data_type"] == "STRING"

    def test_grain_relocates_and_is_time_survives(self) -> None:
        fields = self._fields_by_name(build_osi_document(_make_logical()))
        order_date = fields["order_date"]
        assert order_date["dimension"] == {"is_time": True}
        assert self._fluid_ext(order_date)["grain"] == "day"

    def test_relationship_description_relocates(self) -> None:
        rel = build_osi_document(_make_logical())["semantic_model"][0]["relationships"][0]
        assert "description" not in rel
        assert self._fluid_ext(rel)["description"] == "FK inferred from column naming"

    def test_missing_expression_synthesized_as_column_reference(self) -> None:
        fields = self._fields_by_name(build_osi_document(_make_logical()))
        assert fields["customer_id"]["expression"]["dialects"] == [
            {"dialect": "ANSI_SQL", "expression": "customer_id"}
        ]

    def test_missing_source_defaults_to_dataset_name(self) -> None:
        datasets = build_osi_document(_make_logical())["semantic_model"][0]["datasets"]
        customers = [d for d in datasets if d["name"] == "customers"][0]
        assert customers["source"] == "customers"

    def test_bigquery_dialect_rows_survive_to_the_document(self) -> None:
        """BIGQUERY joined the spec's dialect enum — rows must no longer
        be filtered out of the interchange document."""
        fields = self._fields_by_name(build_osi_document(_make_logical()))
        dialects = {d["dialect"] for d in fields["order_date"]["expression"]["dialects"]}
        assert "BIGQUERY" in dialects


class TestSpecInputForms:
    def test_string_ai_context_coerces_and_validates(self) -> None:
        """The spec allows ``ai_context`` as a plain string at every
        level; fluid canonicalizes it into ``instructions``."""
        model = OSISemanticModel.model_validate(
            {
                "name": "sales",
                "ai_context": "orders, purchases, sales",
                "datasets": [{"name": "orders", "source": "raw.orders", "ai_context": "orders"}],
            }
        )
        assert model.ai_context.instructions == "orders, purchases, sales"
        logical = LogicalDraft(name="sales", technique="flat", osi=model)
        assert validate_osi_document(build_osi_document(logical)) == []

    def test_ai_context_extra_keys_are_preserved(self) -> None:
        """Spec: the structured ai_context form is additionalProperties:
        true — unknown keys must survive to the document."""
        model = OSISemanticModel.model_validate(
            {
                "name": "sales",
                "ai_context": {"instructions": "use me", "verified_queries": ["q1"]},
                "datasets": [{"name": "orders", "source": "raw.orders"}],
            }
        )
        logical = LogicalDraft(name="sales", technique="flat", osi=model)
        doc = build_osi_document(logical)
        assert doc["semantic_model"][0]["ai_context"]["verified_queries"] == ["q1"]
        assert validate_osi_document(doc) == []


class TestValidatorSurfacesNonConformance:
    def test_relationship_without_columns_is_reported(self) -> None:
        logical = _make_logical()
        logical.osi.relationships[0].from_columns = []
        logical.osi.relationships[0].to_columns = []
        issues = validate_osi_document(build_osi_document(logical))
        assert issues, "empty relationship columns must fail the upstream schema"
        assert any("from_columns" in issue for issue in issues)

    def test_conformance_agent_reports_document_issues_as_warnings(self) -> None:
        """Runtime layer: the document check is advisory (warning), the
        Pydantic IR check stays the error gate."""
        from fluid_build.copilot.agents.conformance_agent import ConformanceAgent

        logical = _make_logical()
        logical.osi.relationships[0].from_columns = []
        logical.osi.relationships[0].to_columns = []
        report = ConformanceAgent().run(logical=logical, standards=["osi"])
        assert report.passes is True  # warnings never block
        warnings = [f for f in report.all_findings if f.severity == "warning"]
        assert any("Ossie interchange document" in f.message for f in warnings)

    def test_conformance_agent_clean_on_conformant_draft(self) -> None:
        from fluid_build.copilot.agents.conformance_agent import ConformanceAgent

        report = ConformanceAgent().run(logical=_make_logical(), standards=["osi"])
        assert report.passes is True
        assert report.error_count == 0
        assert report.warning_count == 0


class TestVendoredSchemaProvenance:
    def test_vendored_schema_is_the_upstream_document(self) -> None:
        """Sanity-pin the vendored copy: upstream $id and the 7-value
        dialect enum (incl. the MAQL + BIGQUERY additions)."""
        from importlib import resources

        schema = json.loads(
            resources.files("fluid_build.schemas")
            .joinpath("ossie-osi-schema.json")
            .read_text("utf-8")
        )
        assert schema["$id"] == "https://github.com/apache/ossie/core-spec/osi-schema.json"
        assert sorted(schema["$defs"]["Dialect"]["enum"]) == sorted(
            ["ANSI_SQL", "SNOWFLAKE", "MDX", "TABLEAU", "DATABRICKS", "MAQL", "BIGQUERY"]
        )
        # Vendor is free-form upstream — a closed enum here would mean
        # the vendored copy drifted from the spec.
        assert "enum" not in schema["$defs"]["Vendor"]


class TestSidecarWriteFormats:
    def test_write_auxiliary_artifacts_json_format(self, tmp_path) -> None:
        from fluid_build.cli.forge_data_model import _write_auxiliary_artifacts

        output_path = tmp_path / "contract.fluid.yaml"
        args = SimpleNamespace(
            emit_osi_sidecar=True,
            osi_sidecar_format="json",
            emit_ddl_dir=None,
            emit_dimensional_variants=None,
        )
        _write_auxiliary_artifacts(args, output_path=output_path, logical=_make_logical())
        sidecar = tmp_path / "contract.fluid.yaml.semantics.osi.json"
        assert sidecar.exists()
        document = json.loads(sidecar.read_text("utf-8"))
        assert validate_osi_document(document) == []

    def test_write_auxiliary_artifacts_yaml_default(self, tmp_path) -> None:
        from fluid_build.cli.forge_data_model import _write_auxiliary_artifacts

        output_path = tmp_path / "contract.fluid.yaml"
        args = SimpleNamespace(
            emit_osi_sidecar=True,
            osi_sidecar_format="yaml",
            emit_ddl_dir=None,
            emit_dimensional_variants=None,
        )
        _write_auxiliary_artifacts(args, output_path=output_path, logical=_make_logical())
        sidecar = tmp_path / "contract.fluid.yaml.semantics.osi.yaml"
        assert sidecar.exists()
        document = yaml.safe_load(sidecar.read_text("utf-8"))
        assert validate_osi_document(document) == []
