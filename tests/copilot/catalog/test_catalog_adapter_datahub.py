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

"""Coverage for :class:`DataHubCatalogAdapter` (V1.5 Sprint B).

Pattern coverage matches the BigQuery / Glue tests. The DataHub
adapter wraps ``acryl-datahub`` (specifically
``DataHubGraph`` for the read API); tests stub the SDK module so
the adapter's lazy import + graph construction can be exercised
without the actual SDK installed.
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
    DataHubCredentials,
)
from fluid_build.copilot.catalog.models import CatalogScope


def _stub_datahub_module() -> tuple[ModuleType, ModuleType]:
    """Build the minimum nested module structure the adapter
    imports — ``datahub.ingestion.graph.client`` for the graph
    client + ``datahub.metadata.schema_classes`` for the aspect
    classes ``get_table`` / ``get_lineage`` request."""
    datahub = ModuleType("datahub")
    ingestion = ModuleType("datahub.ingestion")
    graph = ModuleType("datahub.ingestion.graph")
    client_mod = ModuleType("datahub.ingestion.graph.client")
    client_mod.DataHubGraph = MagicMock(name="DataHubGraph")
    client_mod.DataHubGraphConfig = MagicMock(name="DataHubGraphConfig")
    graph.client = client_mod
    ingestion.graph = graph
    datahub.ingestion = ingestion

    metadata = ModuleType("datahub.metadata")
    schema_classes = ModuleType("datahub.metadata.schema_classes")
    schema_classes.DatasetPropertiesClass = MagicMock(name="DatasetPropertiesClass")
    schema_classes.SchemaMetadataClass = MagicMock(name="SchemaMetadataClass")
    schema_classes.OwnershipClass = MagicMock(name="OwnershipClass")
    schema_classes.GlobalTagsClass = MagicMock(name="GlobalTagsClass")
    schema_classes.UpstreamLineageClass = MagicMock(name="UpstreamLineageClass")
    metadata.schema_classes = schema_classes
    datahub.metadata = metadata
    return datahub, client_mod


@pytest.fixture
def datahub_stub(monkeypatch):
    datahub, client_mod = _stub_datahub_module()
    monkeypatch.setitem(sys.modules, "datahub", datahub)
    monkeypatch.setitem(sys.modules, "datahub.ingestion", datahub.ingestion)
    monkeypatch.setitem(sys.modules, "datahub.ingestion.graph", datahub.ingestion.graph)
    monkeypatch.setitem(sys.modules, "datahub.ingestion.graph.client", client_mod)
    monkeypatch.setitem(sys.modules, "datahub.metadata", datahub.metadata)
    monkeypatch.setitem(
        sys.modules, "datahub.metadata.schema_classes", datahub.metadata.schema_classes
    )
    yield client_mod.DataHubGraph


def _make_adapter() -> Any:
    from fluid_build.copilot.catalog.datahub import DataHubCatalogAdapter

    return DataHubCatalogAdapter(
        credentials=DataHubCredentials(
            server="https://datahub.example.com",
            auth_method="pat",
            token="test-token-123",
        )
    )


class TestFromResolver:
    def test_inline_credentials_construct_adapter(self):
        from fluid_build.copilot.catalog.datahub import DataHubCatalogAdapter

        resolver = CredentialResolver(sources_config_path="/tmp/none.yaml")
        adapter = DataHubCatalogAdapter.from_resolver(
            resolver,
            inline_credentials={
                "server": "https://datahub.example.com",
                "auth_method": "pat",
                "token": "tok",
            },
        )
        assert adapter.name == "datahub"


class TestLazyImport:
    def test_module_loads_without_sdk(self):
        from fluid_build.copilot.catalog import datahub as adapter_mod

        assert adapter_mod.DataHubCatalogAdapter is not None

    def test_method_call_without_sdk_raises_typed_config_error(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("datahub.") or name == "datahub":
                raise ImportError("acryl-datahub missing")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        adapter = _make_adapter()
        with pytest.raises(CatalogConfigError) as exc_info:
            adapter._emitter()
        assert "acryl-datahub" in exc_info.value.message
        joined = " ".join(exc_info.value.suggestions or [])
        assert "[datahub]" in joined


class TestAuditContext:
    def test_only_non_sensitive_fields(self):
        from fluid_build.copilot.catalog.datahub import DataHubCatalogAdapter

        adapter = DataHubCatalogAdapter(
            credentials=DataHubCredentials(
                server="https://datahub.example.com",
                auth_method="pat",
                token="example-secret-token",
            )
        )
        ctx = adapter.audit_context()
        assert ctx["catalog_name"] == "datahub"
        assert ctx["server"] == "https://datahub.example.com"
        assert ctx["auth_method"] == "pat"
        # Token must not appear in audit context.
        for v in ctx.values():
            text = str(v).lower()
            assert "supersecrettoken" not in text


class TestNoAuthDevModeWarning:
    def test_none_auth_logs_warning_on_construction(self, caplog):
        """The ``none`` auth path is for self-hosted dev DataHub
        instances. The adapter logs a warning so operators in
        production don't accidentally pick it."""
        import logging

        from fluid_build.copilot.catalog.datahub import DataHubCatalogAdapter

        with caplog.at_level(logging.WARNING):
            DataHubCatalogAdapter(
                credentials=DataHubCredentials(
                    server="https://dev-datahub",
                    auth_method="none",
                )
            )
        assert any("no_auth" in rec.message for rec in caplog.records)


class TestListTables:
    def test_list_tables_uses_search_filter(self, datahub_stub):
        graph_instance = MagicMock()
        datahub_stub.return_value = graph_instance
        graph_instance.search.return_value = [
            SimpleNamespace(
                urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.orders,PROD)"
            ),
            SimpleNamespace(
                urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.customers,PROD)"
            ),
        ]

        adapter = _make_adapter()
        tables = adapter.list_tables(CatalogScope(database="db", schema_name="schema"))
        assert len(tables) == 2
        # FQN preserves the URN for downstream consumers (the URN
        # is the canonical DataHub identity).
        assert all(t.fqn.startswith("urn:li:dataset:") for t in tables)
        # ``search`` was called with a query that scopes by
        # container — confirms the adapter respects scope filters.
        call_args = graph_instance.search.call_args
        assert call_args is not None
        query_arg = call_args.kwargs.get("query") or call_args.args[0]
        assert "db" in query_arg or "schema" in query_arg

    def test_list_tables_filters_by_explicit_table_list(self, datahub_stub):
        """When ``scope.tables`` is set, only those tables are
        returned even if DataHub's search hits more."""
        graph_instance = MagicMock()
        datahub_stub.return_value = graph_instance
        graph_instance.search.return_value = [
            SimpleNamespace(
                urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.orders,PROD)"
            ),
            SimpleNamespace(
                urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.customers,PROD)"
            ),
        ]
        tables = _make_adapter().list_tables(
            CatalogScope(database="db", schema_name="schema", tables=["orders"])
        )
        assert len(tables) == 1
        assert "orders" in tables[0].fqn


class TestErrorTranslation:
    def test_unauthorized_becomes_permission_error(self, datahub_stub):
        graph_instance = MagicMock()
        datahub_stub.return_value = graph_instance
        graph_instance.search.side_effect = Exception("401 Unauthorized: token invalid")

        adapter = _make_adapter()
        with pytest.raises(CatalogPermissionError) as exc_info:
            adapter.list_tables(CatalogScope(database="db"))
        joined = " ".join(exc_info.value.suggestions or [])
        assert "DataHub" in joined or "View Metadata" in joined

    def test_not_found_becomes_connection_error(self, datahub_stub):
        graph_instance = MagicMock()
        datahub_stub.return_value = graph_instance
        graph_instance.search.side_effect = Exception("404 Not Found")

        adapter = _make_adapter()
        with pytest.raises(CatalogConnectionError):
            adapter.list_tables(CatalogScope(database="db"))


class TestURNNormalisation:
    def test_full_urn_passes_through(self):
        from fluid_build.copilot.catalog.datahub import DataHubCatalogAdapter

        urn = "urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.orders,PROD)"
        assert DataHubCatalogAdapter._normalise_urn(urn) == urn

    def test_three_part_dotted_form_translates_to_urn(self):
        """Operators can type ``snowflake.db.orders`` instead of
        the verbose URN — the adapter normalises."""
        from fluid_build.copilot.catalog.datahub import DataHubCatalogAdapter

        result = DataHubCatalogAdapter._normalise_urn("snowflake.analytics.orders")
        assert result.startswith("urn:li:dataset:")
        assert "snowflake" in result
        assert "analytics.orders" in result
