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

"""Coverage for :class:`GlueCatalogAdapter` (V1.5 Sprint B).

Pattern coverage matches ``test_catalog_adapter_bigquery.py``: every
adapter must honour the same nine patterns, so the tests cover the
same axes (lazy import / from_resolver / audit_context / list_tables
/ get_table / error translation / two-pass fetching / no data values).

The Glue adapter wraps ``boto3``; tests stub
``boto3.session.Session(...).client("glue")`` so the adapter's
``Get*`` API calls run against a controllable mock.
"""

from __future__ import annotations

import sys
from datetime import datetime
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
    GlueCredentials,
)
from fluid_build.copilot.catalog.models import CatalogScope


def _stub_boto3_module() -> ModuleType:
    """Inject a minimal ``boto3`` stub.

    Adapter calls: ``boto3.session.Session(**)`` →
    ``session.client("glue")`` → ``client.get_tables`` /
    ``client.get_table``. The stub provides each layer as a
    MagicMock so the test body can pre-program return values.
    """
    boto3 = ModuleType("boto3")
    session_module = ModuleType("boto3.session")
    session_module.Session = MagicMock(name="boto3.session.Session")
    boto3.session = session_module
    return boto3


@pytest.fixture
def boto3_stub(monkeypatch):
    boto3 = _stub_boto3_module()
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "boto3.session", boto3.session)
    yield boto3.session.Session


def _make_adapter() -> Any:
    from fluid_build.copilot.catalog.glue import GlueCatalogAdapter

    return GlueCatalogAdapter(
        credentials=GlueCredentials(region="us-east-1", auth_method="instance_profile")
    )


class TestFromResolver:
    def test_inline_credentials_construct_adapter(self):
        from fluid_build.copilot.catalog.glue import GlueCatalogAdapter

        resolver = CredentialResolver(sources_config_path="/tmp/none.yaml")
        adapter = GlueCatalogAdapter.from_resolver(
            resolver,
            inline_credentials={"region": "us-east-1", "auth_method": "instance_profile"},
        )
        assert adapter.name == "glue"


class TestLazyImport:
    def test_module_loads_without_boto3(self):
        """boto3 isn't installed in dev — module-level import must
        still succeed because the adapter defers ``import boto3``
        until ``_client``."""
        from fluid_build.copilot.catalog import glue as adapter_mod

        assert adapter_mod.GlueCatalogAdapter is not None

    def test_method_call_without_sdk_raises_typed_config_error(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "boto3" or name.startswith("boto3."):
                raise ImportError("boto3 missing")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        adapter = _make_adapter()
        with pytest.raises(CatalogConfigError) as exc_info:
            adapter._client()
        assert "boto3" in exc_info.value.message
        assert exc_info.value.suggestions


class TestAuditContext:
    def test_only_non_sensitive_fields(self):
        from fluid_build.copilot.catalog.glue import GlueCatalogAdapter

        adapter = GlueCatalogAdapter(
            credentials=GlueCredentials(
                region="eu-west-1",
                auth_method="iam_key",
                access_key_id="AKIA-FAKE",
                secret_access_key="secret-fake",
            )
        )
        ctx = adapter.audit_context()
        assert ctx["catalog_name"] == "glue"
        assert ctx["region"] == "eu-west-1"
        assert ctx["auth_method"] == "iam_key"
        # Secrets MUST NOT appear in audit context.
        for v in ctx.values():
            text = str(v).lower()
            assert "secret-fake" not in text
            assert "akia-fake" not in text  # access key value is also sensitive


class TestListTables:
    def test_list_tables_returns_lightweight_summaries(self, boto3_stub):
        """``list_tables`` walks ``glue.get_tables`` pagination and
        returns ``CatalogTable`` summaries — no per-column /
        per-FK detail (those land in ``get_table``)."""
        session_instance = MagicMock()
        boto3_stub.return_value = session_instance
        glue_client = MagicMock()
        session_instance.client.return_value = glue_client
        glue_client.get_tables.return_value = {
            "TableList": [
                {
                    "Name": "orders",
                    "Description": "order events",
                    "Owner": "data-eng",
                    "Parameters": {"domain": "commerce"},
                    "UpdateTime": datetime(2026, 4, 25),
                },
                {
                    "Name": "customers",
                    "Description": None,
                    "Owner": None,
                    "Parameters": {},
                    "UpdateTime": None,
                },
            ],
        }

        adapter = _make_adapter()
        scope = CatalogScope(database="my_db")
        tables = adapter.list_tables(scope)
        assert len(tables) == 2
        assert {t.name for t in tables} == {"orders", "customers"}
        for t in tables:
            assert t.fqn.startswith("my_db.")
            # No per-column detail at list-time.
            assert t.columns == []

        # Confirm the right Glue API was called and the right region
        # is honoured.
        glue_client.get_tables.assert_called()
        kwargs = glue_client.get_tables.call_args_list[0].kwargs
        assert kwargs["DatabaseName"] == "my_db"

    def test_list_tables_carries_table_description(self, boto3_stub):
        """Glue's ``GetTables`` response carries ``Description`` per
        table. The UX-9 audit observed this metadata being silently
        dropped end-to-end, so we pin it here at the adapter
        boundary: the ``Description`` field MUST land on
        ``CatalogTable.description`` so downstream stages
        (logical_agent → modeler → contract emit) can carry it
        verbatim into the generated contract / model docs."""
        session_instance = MagicMock()
        boto3_stub.return_value = session_instance
        glue_client = MagicMock()
        session_instance.client.return_value = glue_client
        glue_client.get_tables.return_value = {
            "TableList": [
                {
                    "Name": "orders",
                    "Description": "Customer orders table",
                    "Owner": "data-eng",
                    "Parameters": {},
                    "UpdateTime": None,
                },
            ],
        }

        tables = _make_adapter().list_tables(CatalogScope(database="my_db"))

        assert len(tables) == 1
        # Description must propagate verbatim to CatalogTable.description.
        assert tables[0].description == "Customer orders table"

    def test_paginated_list_walks_next_token(self, boto3_stub):
        """When Glue returns a NextToken, the adapter must follow
        it until exhaustion. Otherwise large catalogs silently
        return only the first page."""
        session_instance = MagicMock()
        boto3_stub.return_value = session_instance
        glue_client = MagicMock()
        session_instance.client.return_value = glue_client
        glue_client.get_tables.side_effect = [
            {
                "TableList": [
                    {
                        "Name": "a",
                        "Description": None,
                        "Owner": None,
                        "Parameters": {},
                        "UpdateTime": None,
                    }
                ],
                "NextToken": "page2",
            },
            {
                "TableList": [
                    {
                        "Name": "b",
                        "Description": None,
                        "Owner": None,
                        "Parameters": {},
                        "UpdateTime": None,
                    }
                ],
            },
        ]
        tables = _make_adapter().list_tables(CatalogScope(database="my_db"))
        assert {t.name for t in tables} == {"a", "b"}
        assert glue_client.get_tables.call_count == 2

    def test_missing_database_raises(self):
        adapter = _make_adapter()
        with pytest.raises(CatalogConfigError) as exc_info:
            adapter.list_tables(CatalogScope())
        assert "database" in exc_info.value.message.lower()


class TestGetTable:
    """Pin the Glue → CatalogTable metadata bridge.

    UX-9 audit (catalog descriptions silently dropped end-to-end)
    motivated this test class. The Glue ``GetTable`` response
    carries table-level ``Description`` AND per-column ``Comment``
    fields. Both MUST flow into the resulting :class:`CatalogTable`
    so downstream stages (logical_agent → modeler → contract emit)
    can carry them into the generated artifacts.
    """

    def test_get_table_carries_description_and_column_comments(self, boto3_stub):
        session_instance = MagicMock()
        boto3_stub.return_value = session_instance
        glue_client = MagicMock()
        # lakeformation calls go to a different client; soft-fail
        # path returns {} so the test focuses on Glue-only metadata.
        session_instance.client.return_value = glue_client
        glue_client.get_table.return_value = {
            "Table": {
                "Name": "orders",
                "Description": "Customer orders table",
                "Owner": "data-eng",
                "Parameters": {},
                "UpdateTime": None,
                "StorageDescriptor": {
                    "Columns": [
                        {
                            "Name": "order_id",
                            "Type": "string",
                            "Comment": "primary key",
                        },
                        {
                            "Name": "amount_usd",
                            "Type": "double",
                            "Comment": "Total in USD",
                        },
                        {
                            "Name": "status",
                            "Type": "string",
                            # No Comment — pins the None passthrough.
                        },
                    ]
                },
                "PartitionKeys": [],
            }
        }

        # lakeformation soft-fails (no LF configured in this fake env);
        # the adapter should keep going and still return descriptions.
        # Stub the LF client lookup to raise so safe_metadata_call
        # exercises the fallback path.
        session_instance.client.side_effect = lambda svc: (
            glue_client if svc == "glue" else (_ for _ in ()).throw(Exception("no LF"))
        )

        adapter = _make_adapter()
        table = adapter.get_table("my_db.orders")

        # Table-level Description propagates.
        assert table.description == "Customer orders table"

        # Per-column Comment propagates to CatalogColumn.description.
        col_descs = {c.name: c.description for c in table.columns}
        assert col_descs["order_id"] == "primary key"
        assert col_descs["amount_usd"] == "Total in USD"
        # Missing Comment surfaces as None — not the literal string
        # "None" or an empty string artefact.
        assert col_descs["status"] is None


class TestErrorTranslation:
    def test_access_denied_becomes_permission_error(self, boto3_stub):
        session_instance = MagicMock()
        boto3_stub.return_value = session_instance
        glue_client = MagicMock()
        session_instance.client.return_value = glue_client
        glue_client.get_tables.side_effect = Exception(
            "AccessDeniedException: User not authorized to perform glue:GetTables"
        )

        adapter = _make_adapter()
        with pytest.raises(CatalogPermissionError) as exc_info:
            adapter.list_tables(CatalogScope(database="my_db"))
        joined = " ".join(exc_info.value.suggestions or [])
        assert "AWSGlueConsoleReadOnlyAccess" in joined or "GetDatabase" in joined

    def test_not_found_becomes_connection_error(self, boto3_stub):
        session_instance = MagicMock()
        boto3_stub.return_value = session_instance
        glue_client = MagicMock()
        session_instance.client.return_value = glue_client
        glue_client.get_tables.side_effect = Exception(
            "EntityNotFoundException: Database my_db not found"
        )

        adapter = _make_adapter()
        with pytest.raises(CatalogConnectionError):
            adapter.list_tables(CatalogScope(database="my_db"))


class TestEmptyByDesign:
    def test_get_lineage_returns_empty(self):
        """Glue itself has no table lineage; that lives in AWS Data
        Lineage which is a separate adapter (v1.6+). Empty result
        keeps the consistent shape contract."""
        adapter = _make_adapter()
        lineage = adapter.get_lineage("my_db.orders")
        assert lineage.upstream == []
        assert lineage.downstream == []

    def test_list_glossary_returns_empty(self):
        adapter = _make_adapter()
        terms = adapter.list_glossary_terms(CatalogScope(database="my_db"))
        assert terms == []


class TestFQNParsing:
    def test_two_part_fqn_parses(self):
        from fluid_build.copilot.catalog.glue import GlueCatalogAdapter

        parts = GlueCatalogAdapter._parse_fqn("my_db.orders")
        assert parts == ("my_db", "orders")

    def test_one_part_fqn_raises(self):
        from fluid_build.copilot.catalog.glue import GlueCatalogAdapter

        with pytest.raises(CatalogConfigError) as exc_info:
            GlueCatalogAdapter._parse_fqn("orders")
        assert "DATABASE.TABLE" in exc_info.value.message
