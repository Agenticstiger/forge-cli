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

"""Coverage for V1.5 Sprint E / Gap 7 — ConformanceAgent.

The agent runs deterministic Python checks (no LLM) against a
``LogicalDraft`` / contract dict and returns a typed
``ConformanceReport``. Three behavior contracts are pinned:

1. **Standard dispatch.** Calling ``run(standards=[...])`` runs
   only the named standards; unknown / future names silently
   no-op so a typo doesn't break the agent.
2. **Pass / fail signal.** ``ConformanceReport.passes`` is True
   iff every run standard returned zero error-severity findings.
   Warnings are non-blocking.
3. **Idempotence.** Running twice with the same input produces
   the same report shape — required for the coordinator's
   repair-loop semantics.
"""

from __future__ import annotations

import pytest

from fluid_build.copilot.agents.conformance_agent import (
    SUPPORTED_STANDARDS,
    ConformanceAgent,
    ConformanceReport,
)
from fluid_build.copilot.schemas.osi import (
    OSIAIContext,
    OSIDataset,
    OSIField,
    OSISemanticModel,
)
from fluid_build.copilot.schemas.stage_outputs import (
    ConceptualDraft,
    LogicalDraft,
    ValidationFinding,
)


def _make_logical(*, technique: str = "data_vault_2") -> LogicalDraft:
    """Build a minimum-viable LogicalDraft for the agent to lint
    against. Uses the lightest *document-conformant* OSI shape —
    name + ai_context + one dataset (the Ossie spec requires ≥1
    dataset) — so we exercise the agent's plumbing without any
    per-field LLM-generated complexity.

    The DV2 payload is populated when ``technique=data_vault_2``
    so the Fluid validator's "technique without payload" error
    isn't triggered by accident; tests that WANT that error
    explicitly clear ``logical.dv2``.
    """
    from fluid_build.copilot.schemas.data_model import DV2Model, HubDefinition

    return LogicalDraft(
        name="orders",
        technique=technique,
        conceptual=ConceptualDraft(
            name="orders",
            description="Order analytics",
            entities=[],
            relationships=[],
        ),
        osi=OSISemanticModel(
            name="orders",
            description="Order analytics",
            ai_context=OSIAIContext(
                instructions="Use for revenue analytics.",
                synonyms=["sales", "purchases"],
            ),
            datasets=[
                OSIDataset(
                    name="orders",
                    source="raw.orders",
                    primary_key=["order_id"],
                    fields=[OSIField(name="order_id")],
                )
            ],
            relationships=[],
            metrics=[],
        ),
        dv2=(
            DV2Model(
                hubs=[
                    HubDefinition(
                        entity_name="order",
                        hub_table_name="hub_order",
                        business_key_columns=["order_id"],
                    )
                ],
                links=[],
                satellites=[],
                pits=[],
                bridges=[],
            )
            if technique == "data_vault_2"
            else None
        ),
    )


class TestPublicSurface:
    def test_supported_standards_contains_four_names(self):
        """Pin the four standards the agent advertises so a future
        rename / drop fails the test loudly. The exact set is
        part of the public API."""
        assert set(SUPPORTED_STANDARDS) == {
            "fluid",
            "osi",
            "odcs_translation_readiness",
            "dcs_translation_readiness",
        }

    def test_default_standards_run_is_fluid_plus_osi(self):
        """With no ``standards`` arg, the agent runs the two
        fully-implemented standards. ODCS / DCS are placeholder
        hooks today and stay opt-in until v1.6+ ships them."""
        report = ConformanceAgent().run(logical=_make_logical())
        assert set(report.standards_run) == {"fluid", "osi"}


class TestStandardsDispatch:
    def test_explicit_subset_runs_only_that_subset(self):
        report = ConformanceAgent().run(
            logical=_make_logical(),
            standards=["osi"],
        )
        assert report.standards_run == ["osi"]

    def test_unsupported_standard_silently_dropped(self):
        """A typo'd standard name doesn't break the agent — it's
        silently filtered out. ``standards_run`` records only the
        recognised names so the report shape stays predictable."""
        report = ConformanceAgent().run(
            logical=_make_logical(),
            standards=["fluid", "made_up_standard"],
        )
        assert report.standards_run == ["fluid"]

    def test_odcs_and_dcs_translation_readiness_on_empty_contract(self):
        """ODCS / DCS today aren't full schema validators (that's
        v1.6+). What they DO ship is a translation-readiness
        check: warn when the Fluid contract is missing fields the
        future ODCS / DCS exporter will need.

        Calling without a contract → one warning per standard
        flagging the missing-contract input. NEVER an error
        (translation readiness is observability, not a hard
        gate)."""
        report = ConformanceAgent().run(
            logical=_make_logical(),
            standards=["odcs_translation_readiness", "dcs_translation_readiness"],
        )
        assert report.standards_run == [
            "odcs_translation_readiness",
            "dcs_translation_readiness",
        ]
        # Warnings only — no errors.
        assert report.error_count == 0
        # Each standard surfaces at least one warning when called
        # with a logical-only input (no contract dict).
        assert report.warning_count >= 2
        assert report.passes  # warnings don't break passes

    def test_odcs_passes_clean_on_complete_contract(self):
        """A Fluid contract with description, domain, owner.team,
        and at least one entry in exposes[] passes the ODCS
        translation-readiness check with zero findings — the
        contract is ready to translate without information loss."""
        complete_contract = {
            "description": "Customer orders product",
            "metadata": {
                "domain": "commerce",
                "owner": {"team": "data-eng"},
            },
            "exposes": [{"name": "orders"}],
        }
        report = ConformanceAgent().run(
            logical=_make_logical(),
            contract=complete_contract,
            standards=["odcs_translation_readiness"],
        )
        assert report.passes
        assert "odcs_translation_readiness" not in report.findings_by_standard

    def test_dcs_warns_on_missing_owner_team(self):
        """DCS translation readiness flags missing
        ``metadata.owner.team`` so operators planning a DCS export
        know the field is required."""
        contract_missing_owner = {
            "description": "Customer orders product",
            "metadata": {"domain": "commerce"},
            "exposes": [{"name": "orders"}],
        }
        report = ConformanceAgent().run(
            logical=_make_logical(),
            contract=contract_missing_owner,
            standards=["dcs_translation_readiness"],
        )
        # Warning — not error — because translation readiness is
        # observability-only.
        assert report.warning_count >= 1
        warnings = report.findings_by_standard.get("dcs_translation_readiness", [])
        assert any(
            "metadata.owner.team" in (f.field or "") or "owner.team" in f.message for f in warnings
        )


class TestPassFailSignal:
    def test_clean_logical_passes(self):
        report = ConformanceAgent().run(logical=_make_logical())
        assert report.passes
        assert report.error_count == 0

    def test_invalid_dv2_marker_without_payload_fails(self):
        """The Fluid validator flags a logical that declares
        ``data_vault_2`` without a populated ``dv2`` payload.
        ConformanceAgent picks that up via the ``fluid`` standard."""
        logical = _make_logical()
        # technique=data_vault_2 already; clear dv2 to provoke the
        # error.
        logical.dv2 = None
        report = ConformanceAgent().run(
            logical=logical,
            standards=["fluid"],
        )
        assert not report.passes
        assert report.error_count >= 1


class TestReportShape:
    def test_summary_clean_run(self):
        report = ConformanceAgent().run(logical=_make_logical())
        text = report.summary()
        assert "✓" in text
        assert "clean" in text

    def test_summary_with_errors(self):
        report = ConformanceReport(
            findings_by_standard={
                "fluid": [
                    ValidationFinding(
                        message="x",
                        severity="error",
                        field="exposes",
                    )
                ],
            },
            passes=False,
            standards_run=["fluid"],
        )
        text = report.summary()
        assert "errors=1" in text
        assert "fluid" in text

    def test_all_findings_flat(self):
        report = ConformanceReport(
            findings_by_standard={
                "fluid": [
                    ValidationFinding(message="a", severity="error"),
                    ValidationFinding(message="b", severity="warning"),
                ],
                "osi": [
                    ValidationFinding(message="c", severity="error"),
                ],
            },
            passes=False,
            standards_run=["fluid", "osi"],
        )
        assert len(report.all_findings) == 3
        assert report.error_count == 2
        assert report.warning_count == 1


class TestIdempotence:
    def test_two_runs_same_input_same_output(self):
        """Repair loop runs ConformanceAgent multiple times in a
        single coordinator turn. Two runs against an unchanged
        input must produce identical reports — the agent is
        stateless."""
        agent = ConformanceAgent()
        logical = _make_logical()
        first = agent.run(logical=logical)
        second = agent.run(logical=logical)
        assert first.passes == second.passes
        assert first.error_count == second.error_count
        assert first.warning_count == second.warning_count
        assert first.standards_run == second.standards_run


class TestDialectMapperIntegration:
    """Gap 10 — multi-dialect type-mapper integration via
    :meth:`ConformanceAgent.apply_dialect_mapper`.

    The mapper itself is tested at
    ``tests/forge_datamodel/test_dialect_mapper.py``; this class
    pins the agent-level wiring:

    * Missing dialects produce ``severity="warning"`` findings AND
      back-fill the OSI ``expression.dialects[]`` in place.
    * LLM-emitted dialect values that disagree with the
      deterministic mapper produce a ``warning`` finding (dialect
      drift) — without changing the LLM's value (advisory, not
      authoritative).
    * Empty / no-OSI input returns no findings.
    """

    def _logical_with_dialects(self, dialects):
        from fluid_build.copilot.schemas.osi import (
            OSIDataset,
            OSIExpression,
            OSIField,
        )

        logical = _make_logical()
        logical.osi.datasets = [
            OSIDataset(
                name="orders",
                source="raw.orders",
                fields=[
                    OSIField(
                        name="amount",
                        data_type="DECIMAL",
                        expression=OSIExpression(dialects=dialects),
                    ),
                ],
            )
        ]
        return logical

    def test_missing_dialect_produces_warning_and_backfill(self):
        """LLM emitted only ANSI_SQL — mapper adds the rest. The
        agent emits one warning per added dialect.

        Constrained to the OSI-validated subset
        (``ANSI_SQL | SNOWFLAKE | DATABRICKS``) so the resulting
        ``OSIExpression`` validates after back-fill.
        """
        logical = self._logical_with_dialects(
            [{"dialect": "ANSI_SQL", "expression": "DECIMAL(38,10)"}],
        )
        findings = ConformanceAgent().apply_dialect_mapper(
            logical,
            targets=["ANSI_SQL", "SNOWFLAKE", "DATABRICKS"],
        )

        # At least one back-fill happened.
        assert len(findings) >= 1
        assert all(f.severity == "warning" for f in findings)
        # The OSI was mutated in place — additional dialects added.
        result_dialects = logical.osi.datasets[0].fields[0].expression.dialects
        result_dialect_names = {
            (d.dialect if hasattr(d, "dialect") else d.get("dialect")) for d in result_dialects
        }
        assert len(result_dialect_names) >= 2  # ANSI_SQL plus at least one back-fill

    def test_no_osi_returns_no_findings(self):
        logical = _make_logical()
        logical.osi.datasets = []
        assert ConformanceAgent().apply_dialect_mapper(logical) == []

    def test_none_logical_returns_no_findings(self):
        assert ConformanceAgent().apply_dialect_mapper(None) == []  # type: ignore[arg-type]

    def test_default_targets_restrict_to_osi_supported(self):
        """Gap 4 — calling ``apply_dialect_mapper`` with NO
        ``targets`` kwarg must not produce dialects OSI rejects.

        Before this fix, the agent defaulted to the mapper's
        ``DEFAULT_DIALECTS`` (BIGQUERY + POSTGRES included),
        which Pydantic rejected when the back-fill was written
        back into ``OSIExpression.dialects[]``. Real callers
        either had to pass an explicit ``targets=`` list (ugly)
        or hit a hard validation crash mid-forge. This test
        asserts the agent now filters automatically.
        """
        from fluid_build.copilot.schemas.osi import (
            OSI_SUPPORTED_DIALECTS,
            OSIDataset,
            OSIExpression,
            OSIField,
        )

        logical = _make_logical()
        logical.osi.datasets = [
            OSIDataset(
                name="orders",
                source="raw.orders",
                fields=[
                    OSIField(
                        name="amount",
                        data_type="DECIMAL",
                        expression=OSIExpression(
                            dialects=[
                                {"dialect": "ANSI_SQL", "expression": "DECIMAL(38,10)"},
                            ],
                        ),
                    ),
                ],
            ),
        ]

        # Call without explicit targets.
        ConformanceAgent().apply_dialect_mapper(logical)

        # The OSI was mutated in place — no Pydantic error means
        # every back-filled dialect is in OSI_SUPPORTED_DIALECTS.
        for dataset in logical.osi.datasets:
            for field in dataset.fields:
                for d in field.expression.dialects:
                    name = d.dialect if hasattr(d, "dialect") else d.get("dialect")
                    assert name in OSI_SUPPORTED_DIALECTS, (
                        f"Back-filled dialect {name!r} is not in "
                        f"OSI_SUPPORTED_DIALECTS — Pydantic would reject this."
                    )

    def test_well_formed_dialects_clean(self):
        """When the LLM's dialects already match the deterministic
        mapping AND every target dialect is present, no findings
        emit — the back-fill is a no-op and there's no drift.

        Uses the OSI-accepted dialect set
        (``ANSI_SQL | SNOWFLAKE | DATABRICKS``) so the
        ``OSIExpression`` model validates. The mapper's
        ``DEFAULT_DIALECTS`` is broader (includes BIGQUERY /
        POSTGRES) — that broader set is exercised by
        ``test_dialect_mapper.py`` via the mapper directly, not
        through OSI.
        """
        from fluid_build.copilot.schemas.osi import (
            OSIDataset,
            OSIExpression,
            OSIField,
        )
        from fluid_build.forge_datamodel.sql import DialectMapper

        # Restrict to the OSI-validated dialect set.
        targets = ["ANSI_SQL", "SNOWFLAKE", "DATABRICKS"]
        canonical = DialectMapper().fill_missing_dialects(
            "STRING",
            existing=[],
            targets=targets,
        )

        logical = _make_logical()
        logical.osi.datasets = [
            OSIDataset(
                name="orders",
                source="raw.orders",
                fields=[
                    OSIField(
                        name="customer_name",
                        data_type="STRING",
                        expression=OSIExpression(dialects=canonical),
                    ),
                ],
            )
        ]
        # Apply with only OSI-supported targets; expect no findings
        # because every entry already matches the canonical mapping.
        findings = ConformanceAgent().apply_dialect_mapper(
            logical,
            targets=targets,
        )
        assert findings == []


class TestNoLlmCalls:
    def test_constructed_without_session(self):
        """The agent has zero LLM dependencies — no session, no
        provider, no API key. Constructible in any environment
        including air-gapped CI.

        ``fluid_version`` defaults to ``FluidSchemaManager.latest_bundled_version()``
        so the agent automatically tracks new schema releases.
        """
        from fluid_build.schema_manager import FluidSchemaManager

        agent = ConformanceAgent()
        assert agent.fluid_version == FluidSchemaManager.latest_bundled_version()

    def test_run_works_offline(self, monkeypatch):
        """Sanity check — running the agent does NOT touch the
        network. We monkeypatch httpx to raise on use; if the
        agent secretly made an HTTP call the test would fail."""
        import httpx

        def boom(*args, **kwargs):
            raise AssertionError("ConformanceAgent must not make network calls")

        monkeypatch.setattr(httpx, "post", boom)
        monkeypatch.setattr(httpx, "get", boom)
        monkeypatch.setattr(httpx, "request", boom)

        report = ConformanceAgent().run(logical=_make_logical())
        # Agent ran cleanly without touching network — pass.
        assert report.passes
