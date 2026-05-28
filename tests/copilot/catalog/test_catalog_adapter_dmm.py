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

"""Coverage for :class:`DataMeshManagerCatalogAdapter` (V1.5 Sprint B).

DMM uses the REST API directly (no separate SDK — ``httpx`` is
already a core forge-cli dep). Tests stub ``httpx.Client`` to
control DMM's responses without reaching the network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.copilot.catalog.base import (
    CatalogConnectionError,
    CatalogPermissionError,
)
from fluid_build.copilot.catalog.credentials import (
    CredentialResolver,
    DataMeshManagerCredentials,
)
from fluid_build.copilot.catalog.models import CatalogScope


def _make_adapter() -> Any:
    from fluid_build.copilot.catalog.datamesh_manager import (
        DataMeshManagerCatalogAdapter,
    )

    return DataMeshManagerCatalogAdapter(
        credentials=DataMeshManagerCredentials(
            server="https://api.datamesh-manager.test",
            api_key="test-api-key-123",
        )
    )


def _stub_httpx_response(status_code: int, payload: Any = None) -> MagicMock:
    """Build a fake ``httpx.Response`` that ``raise_for_status``
    promotes to an HTTPStatusError when ``status_code >= 400``."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": "application/json"}

    if status_code >= 400:

        def raise_for_status():
            import httpx

            raise httpx.HTTPStatusError(
                f"{status_code}",
                request=MagicMock(),
                response=resp,
            )

        resp.raise_for_status.side_effect = raise_for_status
    else:
        resp.raise_for_status.return_value = None

    resp.json.return_value = payload or {}
    resp.text = ""
    return resp


def _stub_httpx_client(responses):
    """Returns a context manager that yields a client whose
    ``request`` cycles through the supplied response list (or
    returns the single response if a single response was given).
    """
    response_iter = iter(responses) if isinstance(responses, list) else None

    client = MagicMock()
    if response_iter is not None:
        client.request.side_effect = lambda *args, **kwargs: next(response_iter)
    else:
        client.request.return_value = responses

    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False
    return cm, client


class TestFromResolver:
    def test_inline_credentials_construct_adapter(self):
        from fluid_build.copilot.catalog.datamesh_manager import (
            DataMeshManagerCatalogAdapter,
        )

        resolver = CredentialResolver(sources_config_path="/tmp/none.yaml")
        adapter = DataMeshManagerCatalogAdapter.from_resolver(
            resolver,
            inline_credentials={
                "server": "https://api.test",
                "api_key": "k",
            },
        )
        assert adapter.name == "datamesh_manager"


class TestAuditContext:
    def test_only_non_sensitive_fields(self):
        adapter = _make_adapter()
        ctx = adapter.audit_context()
        assert ctx["catalog_name"] == "datamesh_manager"
        assert ctx["server"] == "https://api.datamesh-manager.test"
        # API key MUST NOT appear in audit context.
        for v in ctx.values():
            assert "test-api-key" not in str(v)


class TestListTables:
    def test_list_tables_returns_data_products_as_tables(self):
        cm, client = _stub_httpx_client(
            _stub_httpx_response(
                200,
                {
                    "dataProducts": [
                        {
                            "id": "customer-orders",
                            "name": "Customer Orders",
                            "description": "Order facts",
                            "owner": "data-eng",
                            "domain": "commerce",
                            "status": "active",
                        },
                        {
                            "id": "product-catalog",
                            "name": "Product Catalog",
                            "domain": "commerce",
                        },
                    ]
                },
            )
        )

        with patch("httpx.Client", return_value=cm):
            adapter = _make_adapter()
            tables = adapter.list_tables(CatalogScope())

        assert len(tables) == 2
        assert {t.fqn for t in tables} == {"customer-orders", "product-catalog"}
        # Bearer auth header was passed.
        call_kwargs = client.request.call_args.kwargs
        assert call_kwargs["headers"]["Authorization"] == "Bearer test-api-key-123"

    def test_domain_scope_propagates_to_query_params(self):
        cm, client = _stub_httpx_client(_stub_httpx_response(200, []))

        with patch("httpx.Client", return_value=cm):
            adapter = _make_adapter()
            adapter.list_tables(CatalogScope(database="commerce"))

        params = client.request.call_args.kwargs.get("params") or {}
        # Adapter passes ``database`` as the DMM ``domain`` query param.
        assert params == {"domain": "commerce"}


class TestErrorTranslation:
    def test_401_becomes_permission_error(self):
        cm, _ = _stub_httpx_client(_stub_httpx_response(401))

        with patch("httpx.Client", return_value=cm):
            adapter = _make_adapter()
            with pytest.raises(CatalogPermissionError):
                adapter.list_tables(CatalogScope())

    def test_404_becomes_connection_error(self):
        """404 indicates a not-found resource — translate to
        ``CatalogConnectionError`` (not ``CatalogPermissionError``)
        so the operator's next-action is "verify the URL", not
        "check IAM"."""
        cm, _ = _stub_httpx_client(_stub_httpx_response(404))

        with patch("httpx.Client", return_value=cm):
            adapter = _make_adapter()
            with pytest.raises(CatalogConnectionError):
                adapter.list_tables(CatalogScope())


class TestPerCallClientLifecycle:
    def test_client_closes_after_call(self):
        """Pattern 3 — per-call client lifecycle. The
        ``httpx.Client`` context manager exits after the request,
        ensuring no MCP-server-spanning connection state."""
        cm, client = _stub_httpx_client(_stub_httpx_response(200, {"dataProducts": []}))

        with patch("httpx.Client", return_value=cm):
            adapter = _make_adapter()
            adapter.list_tables(CatalogScope())

        # Context manager's __exit__ was called → connection
        # closed. Pattern 3 satisfied.
        cm.__exit__.assert_called()


class TestDmmApiPaths:
    """Pin the DMM REST paths against the vendor's published surface.

    Regression guard against the v1.5 mistake where the adapter
    called ``/api/data-products`` (with hyphen) — a 404 against every
    real DMM instance. The publisher side at
    ``providers/datamesh_manager`` uses ``/api/dataproducts`` and
    ``/api/datacontracts``; the read-side adapter must match.
    """

    def test_list_tables_calls_dataproducts_without_hyphen(self):
        cm, client = _stub_httpx_client(_stub_httpx_response(200, {"dataProducts": []}))
        with patch("httpx.Client", return_value=cm):
            _make_adapter().list_tables(CatalogScope())
        # client.request is called positionally as (method, url, ...)
        args = client.request.call_args.args
        assert args[0] == "GET"
        # Path is /api/dataproducts — not /api/data-products.
        assert args[1].endswith("/api/dataproducts")
        assert "/api/data-products" not in args[1]

    def test_get_table_calls_dataproducts_then_datacontracts(self):
        # First call: product fetch; second call: contract fetch.
        cm, client = _stub_httpx_client(
            [
                _stub_httpx_response(200, {"name": "p", "outputPorts": []}),
                _stub_httpx_response(200, {"id": "p", "schema": []}),
            ]
        )
        with patch("httpx.Client", return_value=cm):
            _make_adapter().get_table("commerce.orders")

        product_call = client.request.call_args_list[0]
        contract_call = client.request.call_args_list[1]
        # Product fetch hits /api/dataproducts/{fqn}.
        assert product_call.args[1].endswith("/api/dataproducts/commerce.orders")
        # Contract fetch hits /api/datacontracts/{fqn} — the canonical
        # DMM contract path, NOT a /contract sub-resource on the
        # product.
        assert contract_call.args[1].endswith("/api/datacontracts/commerce.orders")
        assert "/api/data-products" not in product_call.args[1]
        assert "/api/data-products" not in contract_call.args[1]

    def test_get_lineage_calls_dataproducts_lineage(self):
        cm, client = _stub_httpx_client(
            _stub_httpx_response(200, {"upstream": [], "downstream": []})
        )
        with patch("httpx.Client", return_value=cm):
            _make_adapter().get_lineage("commerce.orders")
        url = client.request.call_args.args[1]
        assert url.endswith("/api/dataproducts/commerce.orders/lineage")
        assert "/api/data-products" not in url

    def test_list_glossary_terms_returns_empty_without_calling_dmm(self):
        """DMM has no glossary endpoint. The adapter MUST NOT hit the
        network (the v1.5 mistake was to call ``/api/glossary``,
        which always 404s); it should return ``[]`` synchronously."""
        cm, client = _stub_httpx_client(_stub_httpx_response(404))
        with patch("httpx.Client", return_value=cm):
            terms = _make_adapter().list_glossary_terms(CatalogScope())
        assert terms == []
        # No HTTP call was made — adapter knew DMM has no glossary
        # endpoint and short-circuited.
        client.request.assert_not_called()

    def test_translation_suggestion_points_at_correct_url(self):
        """When DMM 404s, the error suggestion must reference the
        right URL — not the v1.5 ``/api/data-products`` typo that
        sent operators down the wrong rabbit hole."""
        cm, _ = _stub_httpx_client(_stub_httpx_response(404))
        with patch("httpx.Client", return_value=cm):
            adapter = _make_adapter()
            with pytest.raises(CatalogConnectionError) as exc_info:
                adapter.list_tables(CatalogScope())
        joined = " ".join(exc_info.value.suggestions or [])
        # The connection-level suggestion (always emitted when the
        # error isn't recognised as a permission failure) hints
        # ``/api/dataproducts`` as the smoke-test URL. The 404-path
        # ``target`` references the request URL, which is also the
        # correct ``/api/dataproducts`` path. Either way, the wrong
        # ``/api/data-products`` string must not appear.
        assert "/api/data-products" not in joined
        assert "/api/data-products" not in exc_info.value.message
