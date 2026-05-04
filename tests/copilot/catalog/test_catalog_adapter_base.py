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

"""Coverage for the V1.5 :class:`CatalogAdapter` ABC + Pydantic shapes.

These tests pin the public contract every catalog implementation must
honour:

1. ABC enforces the four read-only methods (``list_tables``,
   ``get_table``, ``get_lineage``, ``list_glossary_terms``).
2. Subclasses that miss one of the abstract methods can't be
   instantiated — Python raises ``TypeError``.
3. ``audit_context`` returns non-sensitive metadata only (the V1.5
   security guarantee — credential VALUES never appear here).
4. Pydantic shapes round-trip through ``model_dump`` /
   ``model_validate`` losslessly with both alias forms (the
   ``schema`` / ``schema_name`` shadow workaround).
5. Typed exceptions inherit from :class:`FluidError` so existing
   ``except FluidError:`` handlers keep catching catalog failures.
"""

from __future__ import annotations

from typing import List

import pytest

from fluid_build.copilot.catalog import (
    CatalogAdapter,
    CatalogColumn,
    CatalogConfigError,
    CatalogConnectionError,
    CatalogForeignKey,
    CatalogLineage,
    CatalogPermissionError,
    CatalogScope,
    CatalogTable,
    GlossaryTerm,
    LineageRef,
    SensitivityTag,
)
from fluid_build.copilot.catalog.base import CatalogError
from fluid_build.errors import FluidError

# ----------------------------------------------------------------------
# ABC enforcement
# ----------------------------------------------------------------------


class _ConformantAdapter(CatalogAdapter):
    """Minimal valid CatalogAdapter — every abstract method
    implemented. Used to verify the ABC's positive path."""

    name = "conformant"

    def list_tables(self, scope):
        return []

    def get_table(self, fqn):
        return CatalogTable(fqn=fqn, name=fqn.split(".")[-1])

    def get_lineage(self, fqn):
        return CatalogLineage()

    def list_glossary_terms(self, scope):
        return []


class TestCatalogAdapterABC:
    def test_conformant_subclass_instantiates(self):
        adapter = _ConformantAdapter()
        assert adapter.name == "conformant"
        # Abstract methods all callable.
        assert adapter.list_tables(CatalogScope()) == []
        assert adapter.get_lineage("a.b.c") == CatalogLineage()

    def test_missing_abstract_method_blocks_instantiation(self):
        """A catalog adapter that forgets to implement
        ``list_tables`` must fail at class-construction time, not
        at first call. The ABC's whole point."""
        with pytest.raises(TypeError, match="abstract"):

            class _Incomplete(CatalogAdapter):
                name = "incomplete"

                def get_table(self, fqn):
                    return CatalogTable(fqn=fqn, name="x")

                def get_lineage(self, fqn):
                    return CatalogLineage()

                def list_glossary_terms(self, scope):
                    return []

            _Incomplete()

    def test_audit_context_default_is_non_sensitive(self):
        """The default ``audit_context`` returns just the catalog
        name. V1.5 requires NO secrets in audit events; the default
        path must be safe."""
        adapter = _ConformantAdapter()
        ctx = adapter.audit_context()
        assert ctx == {"catalog_name": "conformant"}

    def test_permission_error_helper_carries_suggestions(self):
        """The shared helper builds typed errors with actionable
        ``suggestions`` so every catalog produces consistent UX."""
        err = CatalogAdapter._permission_error(
            "Snowflake denied a metadata read",
            privilege="USAGE on schema DEMO_DB.SEEDED",
            grant_sql="GRANT USAGE ON SCHEMA DEMO_DB.SEEDED TO ROLE ANALYST;",
        )
        assert isinstance(err, CatalogPermissionError)
        assert err.suggestions
        # Suggestions name BOTH the privilege AND the grant SQL.
        text = " ".join(err.suggestions)
        assert "USAGE" in text
        assert "GRANT USAGE" in text


# ----------------------------------------------------------------------
# Typed exception hierarchy
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls",
    [
        CatalogError,
        CatalogConnectionError,
        CatalogPermissionError,
        CatalogConfigError,
    ],
)
def test_every_catalog_error_inherits_fluid_error(exc_cls):
    """Legacy ``except FluidError:`` handlers must keep catching the
    new catalog errors."""
    assert issubclass(exc_cls, FluidError)


def test_specific_catalog_errors_inherit_catalog_error():
    assert issubclass(CatalogConnectionError, CatalogError)
    assert issubclass(CatalogPermissionError, CatalogError)
    assert issubclass(CatalogConfigError, CatalogError)


# ----------------------------------------------------------------------
# Pydantic shape round-trips
# ----------------------------------------------------------------------


class TestCatalogScopeAlias:
    def test_alias_form_accepted(self):
        """JSON callers pass ``schema`` (the alias). Python callers
        pass ``schema_name`` (the canonical attribute). Both must
        produce the same Pydantic state."""
        s1 = CatalogScope(database="DB", schema="SCH")  # alias
        s2 = CatalogScope(database="DB", schema_name="SCH")
        assert s1.schema_name == s2.schema_name == "SCH"

    def test_dump_by_alias_uses_schema_key(self):
        s = CatalogScope(database="DB", schema_name="SCH")
        dumped = s.model_dump(by_alias=True)
        assert dumped["schema"] == "SCH"
        assert "schema_name" not in dumped

    def test_dump_default_uses_canonical_key(self):
        s = CatalogScope(database="DB", schema_name="SCH")
        dumped = s.model_dump()
        assert dumped["schema_name"] == "SCH"


class TestCatalogTableRoundTrip:
    def test_full_round_trip_preserves_every_field(self):
        original = CatalogTable(
            fqn="DB.SCH.ORDERS",
            database="DB",
            schema_name="SCH",
            name="ORDERS",
            description="Customer orders",
            owner="data-team",
            domain="commerce",
            tags={"pii": "true", "domain": "party"},
            classifications=["PII", "GDPR"],
            sensitivity_tags=[SensitivityTag.PII, SensitivityTag.GDPR],
            certification_level="certified",
            primary_key_columns=["order_id"],
            foreign_keys=[
                CatalogForeignKey(
                    constraint_name="fk_orders_customers",
                    from_columns=["customer_id"],
                    to_table="DB.SCH.CUSTOMERS",
                    to_columns=["id"],
                )
            ],
            partition_keys=["order_date"],
            clustering_keys=["customer_id"],
            columns=[
                CatalogColumn(
                    name="order_id",
                    data_type="NUMBER",
                    nullable=False,
                    primary_key=True,
                    description="Unique order id",
                ),
                CatalogColumn(
                    name="email",
                    data_type="VARCHAR",
                    nullable=True,
                    description="Masked customer email",
                    sensitivity_tags=[SensitivityTag.PII],
                    mask_expression="MASK_EMAIL_POLICY",
                ),
            ],
            lineage=CatalogLineage(
                upstream=[LineageRef(fqn="raw.ingest.orders_raw", kind="upstream")],
                downstream=[LineageRef(fqn="marts.finance.revenue_summary", kind="downstream")],
            ),
            data_quality_score=0.95,
            freshness_sla="PT1H",
            quality_rules=["not_null:order_id", "unique:order_id"],
            data_residency="EU",
            compliance_profile="gdpr",
        )
        # Round-trip: dump → re-validate.
        as_json = original.model_dump_json(by_alias=True)
        restored = CatalogTable.model_validate_json(as_json)
        assert restored == original

    def test_minimal_table_round_trip(self):
        """Catalogs that only know name + FQN must still produce a
        valid CatalogTable — defaults fill the rest."""
        original = CatalogTable(fqn="x.y.z", name="z")
        restored = CatalogTable.model_validate_json(original.model_dump_json())
        assert restored.fqn == "x.y.z"
        assert restored.columns == []
        assert restored.lineage is None


class TestSensitivityTag:
    def test_values_are_string_constants(self):
        """The string values are the audit-trail wire format. Pin
        them so a later refactor doesn't silently rename ``"pii"`` to
        ``"PII"`` and break downstream consumers."""
        assert SensitivityTag.PII.value == "pii"
        assert SensitivityTag.PHI.value == "phi"
        assert SensitivityTag.PCI.value == "pci"
        assert SensitivityTag.GDPR.value == "gdpr"
        assert SensitivityTag.HIPAA.value == "hipaa"

    def test_round_trip_via_string_value(self):
        """Pydantic accepts the string value when reconstructing —
        downstream JSON consumers store strings, not enum instances."""
        col = CatalogColumn(
            name="x",
            data_type="STRING",
            sensitivity_tags=["pii", "phi"],
        )
        assert col.sensitivity_tags == [SensitivityTag.PII, SensitivityTag.PHI]


class TestGlossaryTerm:
    def test_term_with_synonyms_and_examples(self):
        term = GlossaryTerm(
            term="Customer",
            definition="An individual or organisation that places orders.",
            synonyms=["client", "buyer"],
            examples=["John Smith", "Acme Corp."],
            domain="commerce",
        )
        # Round-trip preserves synonyms.
        restored = GlossaryTerm.model_validate(term.model_dump())
        assert restored == term


class TestCatalogLineage:
    def test_default_is_empty_lists(self):
        """Adapters return empty lists rather than ``None`` so
        consumers don't have to defensively check."""
        lineage = CatalogLineage()
        assert lineage.upstream == []
        assert lineage.downstream == []

    def test_lineage_kind_constraint(self):
        """``LineageRef.kind`` is a ``Literal`` — invalid values
        fail at validation time."""
        with pytest.raises(Exception):  # ValidationError
            LineageRef(fqn="x", kind="sideways")  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Public surface — every documented name is importable from the
# package's ``__init__``.
# ----------------------------------------------------------------------


def test_public_surface_is_complete():
    """The package's ``__all__`` must include every name documented
    in the V1.5 plan. Removing one without a deprecation cycle
    breaks downstream community contributors building new
    adapters."""
    import fluid_build.copilot.catalog as catalog_mod

    expected = {
        "CatalogAdapter",
        "CatalogColumn",
        "CatalogForeignKey",
        "CatalogLineage",
        "CatalogScope",
        "CatalogTable",
        "GlossaryTerm",
        "LineageRef",
        "SensitivityTag",
        "CatalogConfigError",
        "CatalogConnectionError",
        "CatalogPermissionError",
    }
    actual = set(catalog_mod.__all__)
    missing = expected - actual
    assert not missing, f"missing public exports: {missing}"


def test_every_adapter_exposes_from_resolver():
    """Mediocre #6 pin: every concrete catalog adapter has a
    ``from_resolver`` classmethod. Without this, a future adapter
    could be added that requires raw-credential ``__init__`` calls
    (defeating the resolver-chain security model) without anyone
    noticing."""
    from fluid_build.copilot.catalog.bigquery import BigQueryCatalogAdapter
    from fluid_build.copilot.catalog.datahub import DataHubCatalogAdapter
    from fluid_build.copilot.catalog.datamesh_manager import (
        DataMeshManagerCatalogAdapter,
    )
    from fluid_build.copilot.catalog.dataplex import DataplexCatalogAdapter
    from fluid_build.copilot.catalog.glue import GlueCatalogAdapter
    from fluid_build.copilot.catalog.snowflake import SnowflakeCatalogAdapter
    from fluid_build.copilot.catalog.unity import UnityCatalogAdapter

    every_adapter = [
        BigQueryCatalogAdapter,
        DataHubCatalogAdapter,
        DataMeshManagerCatalogAdapter,
        DataplexCatalogAdapter,
        GlueCatalogAdapter,
        SnowflakeCatalogAdapter,
        UnityCatalogAdapter,
    ]
    for adapter_cls in every_adapter:
        assert hasattr(adapter_cls, "from_resolver"), (
            f"{adapter_cls.__name__} is missing ``from_resolver``. "
            "This is the canonical construction path — every adapter "
            "must expose it so production code (CLI, MCP) can route "
            "through CredentialResolver. Direct ``__init__`` use is "
            "for tests / one-off scripts only."
        )
        assert callable(adapter_cls.from_resolver)
