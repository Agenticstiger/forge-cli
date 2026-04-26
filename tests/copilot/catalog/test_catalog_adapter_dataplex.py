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

"""Coverage for :class:`DataplexCatalogAdapter` (V1.5 Sprint B).

The Dataplex adapter wraps Google's ``dataplex_v1`` SDK across three
clients (catalog / lineage / glossary). Tests stub the entire SDK
module so the adapter's lazy import + 3-client construction can
be exercised offline.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from fluid_build.copilot.catalog.base import (
    CatalogConfigError,
    CatalogConnectionError,
    CatalogPermissionError,
)
from fluid_build.copilot.catalog.credentials import (
    CredentialResolver,
    DataplexCredentials,
)
from fluid_build.copilot.catalog.models import CatalogScope


def _stub_dataplex_module() -> ModuleType:
    """Build the minimal ``google.cloud.dataplex_v1`` stub."""
    google_module = ModuleType("google")
    google_cloud = ModuleType("google.cloud")
    dataplex_v1 = ModuleType("google.cloud.dataplex_v1")
    dataplex_v1.CatalogServiceClient = MagicMock(name="CatalogServiceClient")
    dataplex_v1.LineageServiceClient = MagicMock(name="LineageServiceClient")
    dataplex_v1.GlossaryServiceClient = MagicMock(name="GlossaryServiceClient")
    google_cloud.dataplex_v1 = dataplex_v1
    google_module.cloud = google_cloud
    return google_module


@pytest.fixture
def dataplex_sdk_stub(monkeypatch):
    google_module = _stub_dataplex_module()
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.cloud", google_module.cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.dataplex_v1", google_module.cloud.dataplex_v1)
    yield google_module.cloud.dataplex_v1


def _make_adapter() -> Any:
    from fluid_build.copilot.catalog.dataplex import DataplexCatalogAdapter

    return DataplexCatalogAdapter(
        credentials=DataplexCredentials(project="my-proj", location="EU", auth_method="adc")
    )


class TestFromResolver:
    def test_inline_credentials_construct_adapter(self):
        from fluid_build.copilot.catalog.dataplex import DataplexCatalogAdapter

        resolver = CredentialResolver(sources_config_path="/tmp/none.yaml")
        adapter = DataplexCatalogAdapter.from_resolver(
            resolver,
            inline_credentials={
                "project": "p",
                "location": "EU",
                "auth_method": "adc",
            },
        )
        assert adapter.name == "dataplex"


class TestLazyImport:
    def test_module_loads_without_sdk(self):
        from fluid_build.copilot.catalog import dataplex as adapter_mod

        assert adapter_mod.DataplexCatalogAdapter is not None

    def test_method_call_without_sdk_raises_typed_config_error(self, monkeypatch):
        # Build the adapter BEFORE installing the import-blocker so the
        # fluid-side module imports cleanly. The blocker only triggers
        # when ``adapter._clients()`` lazy-imports the Google SDK.
        adapter = _make_adapter()

        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("google.cloud.dataplex") or name == "google.cloud.dataplex_v1":
                raise ImportError("google-cloud-dataplex missing")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(CatalogConfigError) as exc_info:
            adapter._clients()
        assert "google-cloud-dataplex" in exc_info.value.message
        joined = " ".join(exc_info.value.suggestions or [])
        assert "[gcp]" in joined


class TestAuditContext:
    def test_only_non_sensitive_fields(self):
        from fluid_build.copilot.catalog.dataplex import DataplexCatalogAdapter

        adapter = DataplexCatalogAdapter(
            credentials=DataplexCredentials(
                project="my-proj",
                location="EU",
                auth_method="service_account_json",
                service_account_path="/etc/sa.json",
            )
        )
        ctx = adapter.audit_context()
        assert ctx["catalog_name"] == "dataplex"
        assert ctx["project"] == "my-proj"
        assert ctx["location"] == "EU"
        assert ctx["auth_method"] == "service_account_json"


class TestThreeClientConstruction:
    def test_clients_dict_holds_all_three_services(self, dataplex_sdk_stub):
        """Pattern 4 — lazy SDK construction. Adapter materialises
        three clients (catalog / lineage / glossary) on first
        ``_clients()`` call and caches the dict for subsequent
        calls."""
        adapter = _make_adapter()
        clients = adapter._clients()
        assert set(clients.keys()) == {"catalog", "lineage", "glossary"}

        # Cached on the instance.
        clients_again = adapter._clients()
        assert clients_again is clients


class TestListTables:
    def test_list_tables_iterates_dataplex_entries(self, dataplex_sdk_stub):
        catalog_instance = MagicMock()
        dataplex_sdk_stub.CatalogServiceClient.return_value = catalog_instance

        catalog_instance.list_entries.return_value = [
            SimpleNamespace(
                name="projects/my-proj/locations/EU/entryGroups/@bigquery/entries/orders",
                fully_qualified_name="bigquery:my-proj.analytics.orders",
                description="order events",
                entry_type="bigquery-table",
            ),
            SimpleNamespace(
                name="projects/my-proj/locations/EU/entryGroups/@bigquery/entries/customers",
                fully_qualified_name="bigquery:my-proj.analytics.customers",
                description=None,
                entry_type="bigquery-table",
            ),
        ]

        adapter = _make_adapter()
        scope = CatalogScope()  # default entry-group is @bigquery
        tables = adapter.list_tables(scope)
        assert len(tables) == 2
        # FQN is the Dataplex resource name (preserves the canonical
        # identity for the lineage / aspect API calls downstream).
        for t in tables:
            assert t.fqn.startswith("projects/my-proj/locations/EU/entryGroups/@bigquery/entries/")

    def test_explicit_catalog_scope_overrides_default_entry_group(self, dataplex_sdk_stub):
        catalog_instance = MagicMock()
        dataplex_sdk_stub.CatalogServiceClient.return_value = catalog_instance
        catalog_instance.list_entries.return_value = []

        adapter = _make_adapter()
        adapter.list_tables(CatalogScope(catalog="@custom-group"))

        # The parent path passed to ``list_entries`` includes the
        # explicit entry-group name from scope.
        kwargs = catalog_instance.list_entries.call_args.kwargs
        assert "@custom-group" in kwargs["parent"]


class TestErrorTranslation:
    def test_permission_denied_translates_with_grant_hint(self, dataplex_sdk_stub):
        catalog_instance = MagicMock()
        dataplex_sdk_stub.CatalogServiceClient.return_value = catalog_instance
        catalog_instance.list_entries.side_effect = Exception(
            "PERMISSION_DENIED: roles/dataplex.metadataReader required"
        )

        adapter = _make_adapter()
        with pytest.raises(CatalogPermissionError) as exc_info:
            adapter.list_tables(CatalogScope())
        joined = " ".join(exc_info.value.suggestions or [])
        assert "roles/dataplex" in joined or "gcloud" in joined


class TestEmptyByDesignFallthroughs:
    def test_lineage_returns_empty_when_lineage_api_missing(self, dataplex_sdk_stub):
        """Soft-fail when the Dataplex lineage API isn't enabled
        on the project — adapter doesn't block the rest of the
        forge."""
        catalog_instance = MagicMock()
        dataplex_sdk_stub.CatalogServiceClient.return_value = catalog_instance
        lineage_instance = MagicMock()
        dataplex_sdk_stub.LineageServiceClient.return_value = lineage_instance
        glossary_instance = MagicMock()
        dataplex_sdk_stub.GlossaryServiceClient.return_value = glossary_instance

        # Lineage call raises — soft-fail returns empty.
        lineage_instance.search_links.side_effect = Exception("Lineage API not enabled")

        adapter = _make_adapter()
        lineage = adapter.get_lineage(
            "projects/my-proj/locations/EU/entryGroups/@bigquery/entries/orders"
        )
        assert lineage.upstream == []
        assert lineage.downstream == []

    def test_glossary_returns_empty_when_no_glossary_set_up(self, dataplex_sdk_stub):
        catalog_instance = MagicMock()
        dataplex_sdk_stub.CatalogServiceClient.return_value = catalog_instance
        lineage_instance = MagicMock()
        dataplex_sdk_stub.LineageServiceClient.return_value = lineage_instance
        glossary_instance = MagicMock()
        dataplex_sdk_stub.GlossaryServiceClient.return_value = glossary_instance

        glossary_instance.list_glossary_terms.side_effect = Exception("Glossary not configured")

        adapter = _make_adapter()
        terms = adapter.list_glossary_terms(CatalogScope())
        assert terms == []
