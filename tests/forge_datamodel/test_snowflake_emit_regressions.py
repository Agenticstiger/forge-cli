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

"""Regression pins for the three Snowflake e2e findings
(``/tmp/fluid-ux-findings/06-snowflake-e2e.md``).

* **H3** — ``emit_ddl_files`` must honour
  ``osi.datasets[*].fields[*].data_type`` instead of hard-coding
  ``STRING`` for every column.
* **H7** — ``build_contract_from_logical`` must default to
  ``binding.platform: snowflake`` (with proper
  database/schema/table location) when the source is a Snowflake
  catalog, not ``local/parquet``.
* **H8** — when ``logical.dv2`` carries N hubs + M links + K
  satellites, the emitted contract must carry N+M+K ``exposes``
  (one per artifact), not a single collapsed expose.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from fluid_build.copilot.agents.logical_agent import _aggregate_catalog_summary
from fluid_build.copilot.catalog.models import CatalogColumn, CatalogScope, CatalogTable
from fluid_build.copilot.schemas.data_model import (
    DV2Model,
    HubDefinition,
    LinkDefinition,
    SatelliteDefinition,
)
from fluid_build.copilot.schemas.osi import (
    OSIDataset,
    OSIField,
    OSISemanticModel,
)
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
from fluid_build.forge_datamodel.emit.ddl import emit_ddl_files
from fluid_build.forge_datamodel.emit.fluid_contract import build_contract_from_logical

# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


def _make_snowflake_typed_logical(
    *,
    extra_hubs: int = 0,
    extra_links: int = 0,
    extra_sats: int = 0,
    with_catalog_summary: bool = True,
) -> LogicalDraft:
    """Build a DV2 LogicalDraft whose OSI sidecar carries proper
    Snowflake types (``TEXT``, ``TIMESTAMP_TZ``, ``NUMBER(15,2)``).

    The H3 regression depends on the DDL emitter being able to look
    up each hub/sat column's type from the OSI field index.

    ``extra_hubs`` / ``extra_links`` / ``extra_sats`` build a larger
    fixture used by the H8 cardinality pin.
    """
    osi_fields = [
        OSIField(name="customer_id", data_type="TEXT"),
        OSIField(name="invoice_date", data_type="TIMESTAMP_TZ"),
        OSIField(name="amount_chf", data_type="NUMBER(15,2)"),
        OSIField(name="session_minutes", data_type="NUMBER(10,0)"),
        OSIField(name="name", data_type="TEXT"),
        OSIField(name="email", data_type="TEXT"),
    ]
    hubs = [
        HubDefinition(
            entity_name="customer",
            hub_table_name="hub_customer",
            business_key_columns=["customer_id"],
            mapped_source_tables=["customer"],
        ),
    ]
    links: List[LinkDefinition] = []
    satellites = [
        SatelliteDefinition(
            entity_name="customer",
            satellite_table_name="sat_customer_profile",
            parent_hub="hub_customer",
            attributes=["name", "email"],
            mapped_source_tables=["customer"],
        ),
    ]

    # Synthesize extra artifacts for cardinality tests.
    for i in range(extra_hubs):
        hubs.append(
            HubDefinition(
                entity_name=f"hub{i}",
                hub_table_name=f"hub_hub{i}",
                business_key_columns=[f"hub{i}_id"],
                mapped_source_tables=[f"hub{i}"],
            )
        )
    for i in range(extra_links):
        links.append(
            LinkDefinition(
                link_name=f"lnk{i}",
                link_table_name=f"lnk_lnk{i}",
                hubs_involved=[f"hub_hub{i}", "hub_customer"],
            )
        )
    for i in range(extra_sats):
        satellites.append(
            SatelliteDefinition(
                entity_name=f"hub{i}",
                satellite_table_name=f"sat_hub{i}_details",
                parent_hub=f"hub_hub{i}",
                attributes=[f"hub{i}_attr"],
                mapped_source_tables=[f"hub{i}"],
            )
        )

    logical = LogicalDraft(
        name="telco_stage_load",
        description="Snowflake-sourced telco stage load",
        technique="data_vault_2",
        dv2=DV2Model(hubs=hubs, links=links, satellites=satellites),
        osi=OSISemanticModel(
            name="telco_stage_load_osi",
            description="Semantic model for telco stage load",
            datasets=[
                OSIDataset(
                    name="customer",
                    source="customer",
                    primary_key=["customer_id"],
                    fields=osi_fields,
                )
            ],
        ),
    )
    if with_catalog_summary:
        logical.source_summary.update(
            {
                "source_kind": "catalog",
                "source_catalog_name": "snowflake",
                "source_database": "TELCO_LAB",
                "source_schema": "TELCO_STAGE_LOAD",
                "source_table_bindings": {
                    "customer": {
                        "database": "TELCO_LAB",
                        "schema": "TELCO_STAGE_LOAD",
                        "table": "CUSTOMER",
                    },
                },
            }
        )
    return logical


# ---------------------------------------------------------------------
# H3 — DDL emitter honours Snowflake column types
# ---------------------------------------------------------------------


class TestH3_DdlEmitterHonoursColumnTypes:
    def test_hub_columns_use_osi_data_types_not_string(self):
        logical = _make_snowflake_typed_logical()
        files = emit_ddl_files(logical)
        hub_ddl = files["hub_customer.sql"]
        # H3: the previous emitter wrote ``customer_id STRING`` — it
        # MUST now write the OSI-sourced ``TEXT`` (the Snowflake
        # adapter's normalized type for this column).
        assert "customer_id TEXT" in hub_ddl
        assert "customer_id STRING" not in hub_ddl

    def test_satellite_columns_use_osi_data_types_not_string(self):
        logical = _make_snowflake_typed_logical()
        files = emit_ddl_files(logical)
        sat_ddl = files["sat_customer_profile.sql"]
        # Both attribute columns must carry their real types.
        assert "name TEXT" in sat_ddl
        assert "email TEXT" in sat_ddl
        # ``STRING`` (the old hard-coded fallback) must not appear
        # for columns that have a known type.
        for column in ("name", "email"):
            assert f"{column} STRING" not in sat_ddl

    def test_complex_snowflake_types_round_trip(self):
        """``NUMBER(15,2)`` and ``TIMESTAMP_TZ`` (the exact types
        called out in the finding) must round-trip verbatim through
        the DDL emitter — no truncation, no STRING fallback."""
        logical = LogicalDraft(
            name="invoices",
            technique="data_vault_2",
            dv2=DV2Model(
                hubs=[
                    HubDefinition(
                        entity_name="invoice",
                        hub_table_name="hub_invoice",
                        business_key_columns=["invoice_id"],
                    )
                ],
                satellites=[
                    SatelliteDefinition(
                        entity_name="invoice",
                        satellite_table_name="sat_invoice_details",
                        parent_hub="hub_invoice",
                        attributes=["amount_chf", "invoice_date", "session_minutes"],
                    )
                ],
            ),
            osi=OSISemanticModel(
                name="invoices",
                datasets=[
                    OSIDataset(
                        name="invoice",
                        primary_key=["invoice_id"],
                        fields=[
                            OSIField(name="invoice_id", data_type="TEXT"),
                            OSIField(name="amount_chf", data_type="NUMBER(15,2)"),
                            OSIField(name="invoice_date", data_type="TIMESTAMP_TZ"),
                            OSIField(name="session_minutes", data_type="NUMBER(10,0)"),
                        ],
                    )
                ],
            ),
        )
        files = emit_ddl_files(logical)
        sat = files["sat_invoice_details.sql"]
        assert "amount_chf NUMBER(15,2)" in sat
        assert "invoice_date TIMESTAMP_TZ" in sat
        assert "session_minutes NUMBER(10,0)" in sat
        # No silent fallback to STRING for these typed columns.
        for forbidden in (
            "amount_chf STRING",
            "invoice_date STRING",
            "session_minutes STRING",
        ):
            assert forbidden not in sat

    def test_unknown_column_falls_back_to_string(self):
        """Columns that the OSI sidecar doesn't know about — common
        for synthesized hash-diff columns and ad-hoc projections —
        still fall back to ``STRING`` so the emitter never emits an
        empty type (``column ;``)."""
        logical = LogicalDraft(
            name="missing_osi",
            technique="data_vault_2",
            dv2=DV2Model(
                hubs=[
                    HubDefinition(
                        entity_name="x",
                        hub_table_name="hub_x",
                        # business_key_columns NOT in OSI fields
                        business_key_columns=["unknown_column"],
                    )
                ],
            ),
            osi=OSISemanticModel(name="missing_osi"),
        )
        files = emit_ddl_files(logical)
        # The fallback path emits STRING — important so the SQL
        # remains parseable even when OSI is sparse.
        assert "unknown_column STRING" in files["hub_x.sql"]

    def test_case_insensitive_column_match(self):
        """Snowflake INFORMATION_SCHEMA returns upper-case identifiers
        (``CUSTOMER_ID``); the modeler often stores them lower-case
        (``customer_id``). The type lookup must be case-folded so
        the link works in both directions."""
        logical = LogicalDraft(
            name="case_test",
            technique="data_vault_2",
            dv2=DV2Model(
                hubs=[
                    HubDefinition(
                        entity_name="customer",
                        hub_table_name="hub_customer",
                        business_key_columns=["CUSTOMER_ID"],
                    )
                ],
            ),
            osi=OSISemanticModel(
                name="case_test",
                datasets=[
                    OSIDataset(
                        name="customer",
                        primary_key=["customer_id"],
                        fields=[OSIField(name="customer_id", data_type="TEXT")],
                    )
                ],
            ),
        )
        files = emit_ddl_files(logical)
        # Upper-case column name in DV2 still resolves via the lower-cased lookup.
        assert "CUSTOMER_ID TEXT" in files["hub_customer.sql"]


# ---------------------------------------------------------------------
# RETEST-6 — Snowflake NUMBER columns must round-trip precision/scale
# from INFORMATION_SCHEMA.COLUMNS through the catalog adapter into the
# DDL emit. H3 fixed the STRING-everywhere bug; this is the followup
# for the bare-``NUMBER`` (no precision/scale) bug in the same family.
# ---------------------------------------------------------------------


class TestRETEST6_NumberPrecisionScaleRoundTrip:
    """``NUMBER(15,2)`` must survive end-to-end: Snowflake metadata →
    :class:`CatalogColumn` → OSI sidecar → DDL emitter. Pre-RETEST-6,
    the Snowflake adapter discarded NUMERIC_PRECISION / NUMERIC_SCALE,
    so DDL emitted ``AMOUNT_CHF NUMBER`` even when the warehouse said
    ``NUMBER(15,2)``. These pins ensure the round-trip works."""

    def test_compose_number_with_precision_and_scale(self):
        """The composition helper produces ``NUMBER(15,2)`` from
        Snowflake's ``DATA_TYPE='NUMBER'``,
        ``NUMERIC_PRECISION=15``, ``NUMERIC_SCALE=2``."""
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale("NUMBER", 15, 2) == "NUMBER(15,2)"

    def test_compose_integer_like_number_with_scale_zero(self):
        """An integer-like NUMBER (precision present, scale=0) emits
        as ``NUMBER(10,0)`` — Snowflake accepts both forms but the
        explicit ``,0`` keeps round-trip equality with what the
        warehouse reported."""
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale("NUMBER", 10, 0) == "NUMBER(10,0)"

    def test_compose_decimal_family_also_parameterised(self):
        """DECIMAL / DEC / NUMERIC are aliases for NUMBER in Snowflake
        but external-table / catalog-imported tables may surface them
        verbatim. The helper accepts the whole family."""
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale("DECIMAL", 18, 4) == "DECIMAL(18,4)"
        assert _compose_data_type_with_precision_scale("NUMERIC", 12, 6) == "NUMERIC(12,6)"
        assert _compose_data_type_with_precision_scale("DEC", 8, 2) == "DEC(8,2)"

    def test_compose_non_number_columns_untouched(self):
        """TEXT / TIMESTAMP_TZ / VARCHAR / BOOLEAN must never get
        spurious ``(p,s)`` appended — Snowflake's information_schema
        leaves NUMERIC_PRECISION null for them, but the defensive
        type-family check is the canonical guard."""
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale("TEXT", None, None) == "TEXT"
        assert _compose_data_type_with_precision_scale("TIMESTAMP_TZ", None, None) == "TIMESTAMP_TZ"
        assert _compose_data_type_with_precision_scale("VARCHAR", None, None) == "VARCHAR"
        assert _compose_data_type_with_precision_scale("BOOLEAN", None, None) == "BOOLEAN"
        # Even if a buggy driver returned a precision for VARCHAR, we
        # do NOT compose ``VARCHAR(80)`` — Snowflake's catalog adapter
        # exposes character_max_length separately (not yet wired) and
        # mixing it with the numeric helper would be wrong.
        assert _compose_data_type_with_precision_scale("VARCHAR", 80, None) == "VARCHAR"

    def test_compose_number_without_precision_keeps_bare(self):
        """When Snowflake reports DATA_TYPE='NUMBER' with neither
        precision nor scale (rare; only happens on degenerate
        external-table metadata), we keep the bare ``NUMBER`` — the
        emitter then falls back to Snowflake's default
        ``NUMBER(38,0)`` semantics. We do NOT invent a precision."""
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale("NUMBER", None, None) == "NUMBER"

    def test_compose_number_with_only_precision_no_scale(self):
        """Precision present but scale null — emit single-arg
        ``NUMBER(p)``. Snowflake interprets this as scale=0 (integer
        semantics)."""
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale("NUMBER", 18, None) == "NUMBER(18)"

    def test_compose_handles_empty_data_type_via_string_fallback(self):
        """If a vendor row comes back with ``DATA_TYPE=''`` / None,
        we fall back to ``STRING`` so the emitted DDL stays parseable
        — matches the broader ``_FALLBACK_TYPE = 'STRING'`` behaviour
        of the DDL emitter."""
        from fluid_build.copilot.catalog.snowflake import (
            _compose_data_type_with_precision_scale,
        )

        assert _compose_data_type_with_precision_scale(None, 15, 2) == "STRING"
        assert _compose_data_type_with_precision_scale("", 15, 2) == "STRING"

    def test_ddl_emit_consumes_composed_number_type(self):
        """End-to-end: when the OSI sidecar carries the *composed*
        type (``NUMBER(15,2)``) — which is what the Snowflake catalog
        adapter now produces — the DDL emitter must round-trip it
        without truncation.

        Pre-RETEST-6 retest-snapshot showed ``AMOUNT_CHF NUMBER``
        (bare) in the emitted DDL because the catalog adapter
        produced bare NUMBER. With the adapter fix in place, the
        composed string flows through and we get
        ``AMOUNT_CHF NUMBER(15,2)``.
        """
        logical = LogicalDraft(
            name="retest6_invoices",
            technique="data_vault_2",
            dv2=DV2Model(
                hubs=[
                    HubDefinition(
                        entity_name="invoice",
                        hub_table_name="hub_invoice",
                        business_key_columns=["invoice_id"],
                    )
                ],
                satellites=[
                    SatelliteDefinition(
                        entity_name="invoice",
                        satellite_table_name="sat_invoice_amounts",
                        parent_hub="hub_invoice",
                        attributes=["amount_chf", "vat_amount", "session_minutes"],
                    )
                ],
            ),
            osi=OSISemanticModel(
                name="retest6_invoices",
                datasets=[
                    OSIDataset(
                        name="invoice",
                        primary_key=["invoice_id"],
                        fields=[
                            OSIField(name="invoice_id", data_type="TEXT"),
                            OSIField(name="amount_chf", data_type="NUMBER(15,2)"),
                            OSIField(name="vat_amount", data_type="DECIMAL(18,4)"),
                            OSIField(name="session_minutes", data_type="NUMBER(10,0)"),
                        ],
                    )
                ],
            ),
        )
        files = emit_ddl_files(logical)
        sat = files["sat_invoice_amounts.sql"]
        # Precision/scale must appear verbatim.
        assert "amount_chf NUMBER(15,2)" in sat
        assert "vat_amount DECIMAL(18,4)" in sat
        assert "session_minutes NUMBER(10,0)" in sat
        # Bare ``NUMBER`` (the RETEST-6 finding) must not appear for
        # these columns — that's the regression we're guarding.
        for forbidden in (
            "amount_chf NUMBER\n",
            "amount_chf NUMBER,",
            "vat_amount DECIMAL\n",
            "vat_amount DECIMAL,",
        ):
            assert forbidden not in sat, (
                f"RETEST-6 regression: bare ``{forbidden.strip(',')}`` "
                "must not appear in the emitted DDL — precision/scale "
                "were discarded."
            )


# ---------------------------------------------------------------------
# H7 — Snowflake-source binding defaults to snowflake/snowflake_table
# ---------------------------------------------------------------------


class TestH7_SnowflakeSourceBinding:
    def test_snowflake_source_emits_snowflake_platform_binding(self):
        logical = _make_snowflake_typed_logical()
        contract = build_contract_from_logical(logical)
        for expose in contract["exposes"]:
            binding = expose["binding"]
            assert binding["platform"] == "snowflake", (
                f"expose {expose.get('exposeId')!r}: Snowflake source must "
                f"emit binding.platform=snowflake, got {binding['platform']!r}"
            )
            assert binding["format"] == "snowflake_table", (
                f"expose {expose.get('exposeId')!r}: Snowflake source must "
                f"emit binding.format=snowflake_table, got {binding['format']!r}"
            )
            # Local/parquet runtime path MUST NOT appear in a
            # Snowflake-sourced contract.
            location = binding.get("location") or {}
            assert (
                "path" not in location
            ), "Snowflake binding must not carry a local filesystem path"

    def test_snowflake_binding_carries_database_schema_table(self):
        """For artifacts mapped to a known source table, the binding
        location must carry ``database`` / ``schema`` / ``table`` so
        IaC emitters and dbt sources can resolve the warehouse object
        without consulting an out-of-band config."""
        logical = _make_snowflake_typed_logical()
        contract = build_contract_from_logical(logical)
        # The customer hub's mapped_source_tables=['customer'] should
        # resolve to the per-table binding hint.
        hub_expose = next(e for e in contract["exposes"] if e["exposeId"] == "hub_customer")
        location = hub_expose["binding"]["location"]
        assert location["database"] == "TELCO_LAB"
        assert location["schema"] == "TELCO_STAGE_LOAD"
        assert location["table"] == "CUSTOMER"

    def test_non_catalog_source_keeps_legacy_local_binding(self):
        """When source_summary is empty (intent / DDL forge — no
        catalog), the emitter must keep the legacy local/parquet
        default so we don't regress existing intent / DDL forges
        into broken Snowflake bindings."""
        logical = _make_snowflake_typed_logical(with_catalog_summary=False)
        contract = build_contract_from_logical(logical)
        for expose in contract["exposes"]:
            binding = expose["binding"]
            assert binding["platform"] == "local"
            assert binding["format"] == "parquet"


# ---------------------------------------------------------------------
# H8 — one expose per DV2 artifact (no collapse)
# ---------------------------------------------------------------------


class TestH8_OneExposePerDV2Artifact:
    def test_three_hub_two_link_three_sat_emits_eight_exposes(self):
        """Per the task spec: a 3-hub / 2-link / 3-sat DV2 source
        must emit exactly 8 ``exposes`` (3 + 2 + 3) — one per
        physical artifact. No more, no fewer."""
        # We already have 1 hub + 0 links + 1 sat in the base
        # fixture, so add 2 / 2 / 2 for the 3+2+3 total.
        logical = _make_snowflake_typed_logical(
            extra_hubs=2,
            extra_links=2,
            extra_sats=2,
        )
        contract = build_contract_from_logical(logical)
        exposes = contract["exposes"]
        assert len(exposes) == 3 + 2 + 3, (
            "DV2 contract must emit one expose per artifact "
            f"(3 hubs + 2 links + 3 sats = 8); got {len(exposes)}"
        )

    def test_each_dv2_expose_carries_artifact_type_label(self):
        """Each emitted expose must label its DV2 artifact type
        so downstream consumers (catalog publishers, dbt-vault
        transformation generators) can group artifacts."""
        logical = _make_snowflake_typed_logical(extra_hubs=2, extra_links=2, extra_sats=2)
        contract = build_contract_from_logical(logical)
        types = [e.get("labels", {}).get("dataVaultArtifactType") for e in contract["exposes"]]
        # Order is hubs → links → sats, so we expect the type
        # progression below.
        assert types.count("hub") == 3
        assert types.count("link") == 2
        assert types.count("satellite") == 3
        # No None entries — every expose must be labeled.
        assert all(t is not None for t in types)

    def test_hub_expose_carries_business_key_columns(self):
        """The hub expose's ``contract.schema`` must carry the
        hub's ``business_key_columns`` — not the columns of some
        other dataset that happened to be last-iterated."""
        logical = _make_snowflake_typed_logical()
        contract = build_contract_from_logical(logical)
        hub_expose = next(e for e in contract["exposes"] if e["exposeId"] == "hub_customer")
        column_names = [c["name"] for c in hub_expose["contract"]["schema"]]
        assert column_names == ["customer_id"]
        # The hub's business_key_columns are primary keys.
        assert hub_expose["contract"]["schema"][0]["required"] is True

    def test_sat_expose_carries_attributes_with_types(self):
        """Sat expose must carry its own attributes (not the hub's
        BKs, not some other dataset's columns) — and each column's
        type must come from the OSI field index."""
        logical = _make_snowflake_typed_logical()
        contract = build_contract_from_logical(logical)
        sat_expose = next(e for e in contract["exposes"] if e["exposeId"] == "sat_customer_profile")
        cols = sat_expose["contract"]["schema"]
        assert [c["name"] for c in cols] == ["name", "email"]
        # Both must be the OSI-sourced TEXT, not the STRING fallback.
        assert all(c["type"] == "TEXT" for c in cols)

    def test_every_expose_passes_schema_validator_constraints(self):
        """The validator requires every expose to carry a non-empty
        ``semantics`` block with name / entities / dimensions /
        measures / metrics. Per-artifact exposes must satisfy that
        contract — otherwise downstream validation breaks."""
        logical = _make_snowflake_typed_logical(extra_hubs=2, extra_links=2, extra_sats=2)
        contract = build_contract_from_logical(logical)
        for expose in contract["exposes"]:
            sem = expose["semantics"]
            assert sem.get("name")
            assert sem.get("entities"), f"{expose['exposeId']}: missing semantics.entities"
            assert sem.get("dimensions"), f"{expose['exposeId']}: missing semantics.dimensions"
            assert sem.get("measures"), f"{expose['exposeId']}: missing semantics.measures"
            assert sem.get("metrics"), f"{expose['exposeId']}: missing semantics.metrics"


# ---------------------------------------------------------------------
# H7-supporting: _aggregate_catalog_summary captures scope + per-table
# bindings so the contract emitter can use them.
# ---------------------------------------------------------------------


class TestAggregateCatalogSummaryCarriesBindingHints:
    def test_scope_database_schema_propagate_to_summary(self):
        scope = CatalogScope(
            database="TELCO_LAB",
            schema_name="TELCO_STAGE_LOAD",
        )
        tables = [
            CatalogTable(
                fqn="TELCO_LAB.TELCO_STAGE_LOAD.CUSTOMER",
                database="TELCO_LAB",
                schema_name="TELCO_STAGE_LOAD",
                name="CUSTOMER",
                columns=[CatalogColumn(name="customer_id", data_type="TEXT")],
            ),
        ]
        summary = _aggregate_catalog_summary(
            adapter_name="snowflake",
            catalog_tables=tables,
            scope=scope,
        )
        assert summary["source_kind"] == "catalog"
        assert summary["source_catalog_name"] == "snowflake"
        assert summary["source_database"] == "TELCO_LAB"
        assert summary["source_schema"] == "TELCO_STAGE_LOAD"
        # Per-table binding keyed by lower-case bare name.
        assert summary["source_table_bindings"]["customer"] == {
            "database": "TELCO_LAB",
            "schema": "TELCO_STAGE_LOAD",
            "table": "CUSTOMER",
        }

    def test_summary_works_without_scope_for_back_compat(self):
        """``_aggregate_catalog_summary`` must accept a missing scope
        so existing callers (no per-table binding) keep working."""
        summary = _aggregate_catalog_summary(
            adapter_name="snowflake",
            catalog_tables=[],
        )
        assert summary["source_kind"] == "catalog"
        assert summary["source_catalog_name"] == "snowflake"
        # No scope → no binding hints surfaced.
        assert "source_database" not in summary
        assert "source_schema" not in summary
        assert "source_table_bindings" not in summary
