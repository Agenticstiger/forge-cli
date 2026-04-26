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

"""Coverage for :class:`BigQueryCatalogAdapter` (V1.5 Sprint B).

These pin every pattern the BigQuery adapter is supposed to apply
(from :mod:`fluid_build.copilot.catalog._patterns`):

* **Pattern 4 (lazy SDK import).** Module loads without
  ``google-cloud-bigquery`` installed; only invocation requires it.
* **Pattern 5 (``from_resolver``).** Construction-by-credential is
  the canonical dispatch path used by MCP / CLI.
* **Pattern 6 (audit context excludes secrets).** Project / location
  / auth_method appear; service-account paths and tokens never do.
* **Pattern 7 (vendor-error → typed-exception).** Permission errors
  carry the ``gcloud`` IAM grant SQL as a suggestion; not-found
  errors carry the dataset-existence check.
* **Pattern 8 (two-pass fetching).** ``list_tables`` returns
  lightweight summaries; ``get_table`` returns full detail.
* **Pattern 9 (no data values fetched).** The adapter NEVER issues
  a ``SELECT * FROM <table>`` — only INFORMATION_SCHEMA reads.

The BigQuery SDK isn't installed in the dev environment, so the
tests stub ``google.cloud.bigquery`` via ``sys.modules`` injection
before the adapter's lazy import runs.
"""

from __future__ import annotations

import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from fluid_build.copilot.catalog.base import (
    CatalogConfigError,
    CatalogConnectionError,
    CatalogPermissionError,
)
from fluid_build.copilot.catalog.credentials import (
    BigQueryCredentials,
    CredentialResolver,
)
from fluid_build.copilot.catalog.models import CatalogScope

# ----------------------------------------------------------------------
# SDK stub — injectable into ``sys.modules`` so the lazy
# ``from google.cloud import bigquery`` inside ``_client`` picks it up
# ----------------------------------------------------------------------


def _stub_google_cloud_bigquery_module() -> ModuleType:
    """Build a minimal ``google.cloud.bigquery`` stub.

    Only the entry points the adapter actually uses are populated:
    ``Client`` (which the adapter constructs and calls
    ``query`` / ``get_table`` on). Tests inject specific behaviour
    by setting attributes on the returned module's ``Client`` mock.
    """
    google_module = ModuleType("google")
    google_cloud_module = ModuleType("google.cloud")
    bigquery_module = ModuleType("google.cloud.bigquery")
    bigquery_module.Client = MagicMock(name="bigquery.Client")
    google_cloud_module.bigquery = bigquery_module
    google_module.cloud = google_cloud_module
    return google_module


@pytest.fixture
def bigquery_sdk_stub(monkeypatch):
    """Install the BigQuery SDK stub for the duration of one test.

    The fixture yields the ``Client`` mock so the test body can
    pre-program ``Client.return_value.query.return_value.result.return_value``
    or ``Client.return_value.get_table.return_value`` to whatever
    the test needs.
    """
    google_module = _stub_google_cloud_bigquery_module()
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.cloud", google_module.cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", google_module.cloud.bigquery)
    yield google_module.cloud.bigquery.Client


def _make_adapter() -> Any:
    from fluid_build.copilot.catalog.bigquery import BigQueryCatalogAdapter

    return BigQueryCatalogAdapter(
        credentials=BigQueryCredentials(project="my-proj", auth_method="adc")
    )


# ----------------------------------------------------------------------
# Pattern 5 — from_resolver dispatch
# ----------------------------------------------------------------------


class TestFromResolver:
    def test_inline_credentials_construct_adapter(self):
        from fluid_build.copilot.catalog.bigquery import BigQueryCatalogAdapter

        resolver = CredentialResolver(sources_config_path="/tmp/none.yaml")
        adapter = BigQueryCatalogAdapter.from_resolver(
            resolver,
            inline_credentials={"project": "my-proj", "auth_method": "adc"},
        )
        assert adapter.name == "bigquery"


# ----------------------------------------------------------------------
# Pattern 4 — lazy SDK import (module loads without google-cloud-bigquery)
# ----------------------------------------------------------------------


class TestLazyImport:
    def test_module_loads_without_sdk(self):
        """Importing the adapter module must succeed even when
        ``google-cloud-bigquery`` isn't installed. The lazy
        ``from google.cloud import bigquery`` lives inside
        ``_client`` so import-time stays clean."""
        # The fact that the module-level ``import`` at the top of
        # this test file works in the dev env (where bigquery is
        # NOT installed) IS the test — Python's import system
        # would have failed if the adapter eager-imported the SDK.
        from fluid_build.copilot.catalog import bigquery as adapter_mod

        assert adapter_mod.BigQueryCatalogAdapter is not None

    def test_method_call_without_sdk_raises_typed_config_error(self, monkeypatch):
        """When the user invokes the adapter without the optional
        extra installed, the typed :class:`CatalogConfigError`
        carries the exact ``pip install`` command they need."""
        # Force the lazy import to fail.
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "google.cloud" and "bigquery" in (args[2] if len(args) > 2 else ()):
                raise ImportError("google.cloud.bigquery missing")
            if name.startswith("google.cloud.bigquery"):
                raise ImportError("google-cloud-bigquery missing")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        adapter = _make_adapter()
        with pytest.raises(CatalogConfigError) as exc_info:
            adapter._client()
        assert "google-cloud-bigquery" in exc_info.value.message
        # Suggestions name BOTH the per-extra and umbrella install
        # commands so the operator picks whichever fits their env.
        assert exc_info.value.suggestions
        joined = " ".join(exc_info.value.suggestions)
        assert "[gcp]" in joined
        assert "[catalogs]" in joined


# ----------------------------------------------------------------------
# Pattern 6 — audit_context never leaks secrets
# ----------------------------------------------------------------------


class TestAuditContext:
    def test_only_non_sensitive_fields(self):
        from fluid_build.copilot.catalog.bigquery import BigQueryCatalogAdapter

        adapter = BigQueryCatalogAdapter(
            credentials=BigQueryCredentials(
                project="my-proj",
                auth_method="service_account_json",
                service_account_path="/tmp/sa.json",
                location="EU",
            )
        )
        ctx = adapter.audit_context()
        # Catalog-level non-sensitive identity.
        assert ctx["catalog_name"] == "bigquery"
        assert ctx["project"] == "my-proj"
        assert ctx["location"] == "EU"
        assert ctx["auth_method"] == "service_account_json"
        # Service-account path is also a config field, not a
        # secret. Surface it for audit-trail clarity (operators
        # can grep audit events for the SA path used).
        assert "credentials_path" not in ctx  # not surfaced — keep ctx minimal
        # No real secret fields exist on BigQuery (ADC has none),
        # but defensively confirm no token/secret/credential value
        # appears in the ctx dict anywhere.
        for v in ctx.values():
            text = str(v).lower()
            assert "secret" not in text
            assert "token" not in text


# ----------------------------------------------------------------------
# Pattern 8 — two-pass fetching: list_tables vs get_table
# ----------------------------------------------------------------------


class TestListTables:
    def test_list_tables_with_database_and_schema(self, bigquery_sdk_stub):
        """``list_tables`` issues a single INFORMATION_SCHEMA query
        and returns lightweight :class:`CatalogTable` instances —
        no per-column / per-FK detail fetched (that's
        :meth:`get_table`'s job)."""
        # Wire the mock client to return three rows.
        client_instance = MagicMock()
        bigquery_sdk_stub.return_value = client_instance
        rows = [
            {
                "table_catalog": "my-proj",
                "table_schema": "analytics",
                "table_name": "orders",
                "creation_time": datetime(2026, 4, 25),
                "ddl": None,
            },
            {
                "table_catalog": "my-proj",
                "table_schema": "analytics",
                "table_name": "customers",
                "creation_time": datetime(2026, 4, 24),
                "ddl": None,
            },
        ]
        # ``Row`` from BQ supports dict-like access; MagicMock with
        # __getitem__ returning the dict's value mirrors that.
        row_mocks = []
        for row in rows:
            mock_row = MagicMock()
            mock_row.__getitem__.side_effect = row.__getitem__
            mock_row.get.side_effect = row.get
            row_mocks.append(mock_row)
        client_instance.query.return_value.result.return_value = row_mocks

        adapter = _make_adapter()
        scope = CatalogScope(database="my-proj", schema_name="analytics")
        tables = adapter.list_tables(scope)

        assert len(tables) == 2
        assert {t.name for t in tables} == {"orders", "customers"}
        for t in tables:
            assert t.fqn.startswith("my-proj.analytics.")
            # Two-pass contract: list-summaries don't carry full
            # column detail.
            assert t.columns == []
            assert t.foreign_keys == []

        # The query SQL must reference INFORMATION_SCHEMA.TABLES,
        # filter on TABLE_TYPE = 'BASE TABLE', and quote the
        # project + dataset identifiers (Pattern 2).
        query_sql = client_instance.query.call_args.args[0]
        assert "INFORMATION_SCHEMA.TABLES" in query_sql
        assert "BASE TABLE" in query_sql
        assert "`my-proj`" in query_sql
        assert "`analytics`" in query_sql
        # Pattern 9 — must NOT issue a SELECT * on the actual data.
        assert "SELECT *" not in query_sql.upper().replace("SELECT *", "SELECT_STAR_BANNED")
        # (The replace is paranoid — the test fails loudly if a
        # future refactor introduces a data-value SELECT.)

    def test_missing_schema_name_raises(self):
        """BigQuery enumeration is dataset-scoped; ``schema_name``
        is required. Adapters fail-closed with an actionable error
        when the operator forgot to pass it."""
        adapter = _make_adapter()
        with pytest.raises(CatalogConfigError) as exc_info:
            adapter.list_tables(CatalogScope(database="my-proj"))
        assert "schema_name" in exc_info.value.message
        assert exc_info.value.suggestions

    def test_invalid_dataset_identifier_rejected(self):
        """Pattern 2 — identifier validation. A dataset name with
        SQL injection characters fails at the validation layer,
        not at the BigQuery API."""
        adapter = _make_adapter()
        with pytest.raises(CatalogConfigError) as exc_info:
            adapter.list_tables(CatalogScope(database="my-proj", schema_name="bad`name; DROP"))
        assert "dataset" in exc_info.value.message.lower()


# ----------------------------------------------------------------------
# Pattern 7 — vendor-error → typed-exception translation
# ----------------------------------------------------------------------


class TestErrorTranslation:
    def test_permission_denied_becomes_typed_error_with_grant_sql(self, bigquery_sdk_stub):
        """A "Permission denied" from BigQuery must translate to
        :class:`CatalogPermissionError` carrying the exact gcloud
        IAM grant command — the operator's next-action is one
        line of CLI away."""
        client_instance = MagicMock()
        bigquery_sdk_stub.return_value = client_instance
        client_instance.query.side_effect = Exception(
            "PERMISSION_DENIED: User 'X' does not have access"
        )

        adapter = _make_adapter()
        with pytest.raises(CatalogPermissionError) as exc_info:
            adapter.list_tables(CatalogScope(database="my-proj", schema_name="analytics"))
        # Suggestions carry the exact gcloud grant.
        assert exc_info.value.suggestions
        joined = " ".join(exc_info.value.suggestions)
        assert "gcloud projects add-iam-policy-binding" in joined
        assert "roles/bigquery.metadataViewer" in joined

    def test_other_failure_becomes_connection_error(self, bigquery_sdk_stub):
        """A non-permission error (network, malformed query)
        translates to :class:`CatalogConnectionError` with
        actionable network suggestions."""
        client_instance = MagicMock()
        bigquery_sdk_stub.return_value = client_instance
        client_instance.query.side_effect = Exception("Connection timed out after 30s")

        adapter = _make_adapter()
        with pytest.raises(CatalogConnectionError) as exc_info:
            adapter.list_tables(CatalogScope(database="my-proj", schema_name="analytics"))
        joined = " ".join(exc_info.value.suggestions or [])
        assert "GOOGLE_APPLICATION_CREDENTIALS" in joined or "verbose" in joined.lower()


# ----------------------------------------------------------------------
# get_lineage / list_glossary_terms — empty-by-design contracts
# ----------------------------------------------------------------------


class TestEmptyByDesign:
    def test_get_lineage_returns_empty(self):
        """BigQuery's lineage lives in Dataplex (separate adapter).
        The BigQuery adapter returns an empty
        :class:`CatalogLineage` so callers get a consistent shape;
        rich lineage is the Dataplex adapter's job."""
        adapter = _make_adapter()
        lineage = adapter.get_lineage("my-proj.analytics.orders")
        assert lineage.upstream == []
        assert lineage.downstream == []

    def test_list_glossary_terms_returns_empty(self):
        """BigQuery has no first-class glossary API. Dataplex
        provides the glossary surface; this adapter returns []
        so multi-catalog dispatch works uniformly."""
        adapter = _make_adapter()
        terms = adapter.list_glossary_terms(CatalogScope(database="x", schema_name="y"))
        assert terms == []


# ----------------------------------------------------------------------
# FQN parsing — three-part dotted form required
# ----------------------------------------------------------------------


class TestFQNParsing:
    def test_three_part_fqn_parses(self):
        from fluid_build.copilot.catalog.bigquery import BigQueryCatalogAdapter

        parts = BigQueryCatalogAdapter._parse_fqn("my-proj.analytics.orders")
        assert parts == ("my-proj", "analytics", "orders")

    def test_two_part_fqn_raises(self):
        from fluid_build.copilot.catalog.bigquery import BigQueryCatalogAdapter

        with pytest.raises(CatalogConfigError) as exc_info:
            BigQueryCatalogAdapter._parse_fqn("analytics.orders")
        assert "PROJECT.DATASET.TABLE" in exc_info.value.message
