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

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from fluid_build.cli.datamesh_manager import _cmd_publish, _publish_exit_code
from fluid_build.providers.base import ProviderError
from fluid_build.providers.datamesh_manager import DataMeshManagerProvider


def _sample_contract():
    return {
        "id": "sales-product",
        "metadata": {
            "name": "Sales Product",
            "description": "demo",
            "status": "active",
            "owner": {"team": "analytics"},
        },
        "owner": {"team": "analytics"},
        "exposes": [],
        "expects": [],
    }


def _sample_contract_with_exposes():
    return {
        "id": "sales-product",
        "metadata": {
            "name": "Sales Product",
            "description": "demo",
            "status": "active",
            "owner": {"team": "analytics"},
        },
        "owner": {"team": "analytics"},
        "exposes": [
            {"id": "orders", "provider": "gcp", "contract": {"schema": []}},
            {"id": "customers", "provider": "gcp", "contract": {"schema": []}},
        ],
        "expects": [],
    }


def _sample_odps_alias_contract():
    return {
        "id": "sales-product",
        "metadata": {
            "name": "Sales Product",
            "description": "demo",
            "status": "active",
            "owner": {"team": "analytics"},
            "type": "analytical",
        },
        "tags": ["sales", "gold"],
        "owner": {"team": "analytics"},
        "exposes": [],
        "expects": [],
    }


def _sample_odps_binding_platform_contract():
    return {
        "id": "sales-product",
        "metadata": {
            "name": "Sales Product",
            "description": "demo",
            "status": "active",
            "owner": {"team": "analytics"},
        },
        "owner": {"team": "analytics"},
        "exposes": [
            {
                "id": "orders",
                "binding": {
                    "platform": "gcp",
                    "location": {
                        "project": "demo-project",
                        "dataset": "sales",
                        "table": "orders",
                    },
                },
                "contract": {"schema": []},
            }
        ],
        "expects": [],
    }


def _sample_odps_consumes_contract():
    return {
        "fluidVersion": "0.7.1",
        "id": "bizlab.teleforge.subscriber_health_360_lineage_local",
        "name": "TeleForge Subscriber Health 360 Local",
        "description": (
            "Gold subscriber health mart built from the Silver usage and billing daily products."
        ),
        "domain": "telco",
        "metadata": {
            "status": "active",
            "owner": {"team": "bizlab", "email": "bizlab@example.com"},
        },
        "consumes": [
            {
                "productId": "bizlab.teleforge.subscriber_usage_daily_lineage_local",
                "exposeId": "subscriber_usage_daily",
                "purpose": "Supply daily subscriber usage features to the health model.",
            },
            {
                "productId": "bizlab.teleforge.billing_health_daily_lineage_local",
                "exposeId": "billing_health_daily",
                "purpose": "Supply payment behavior and overdue indicators to the health model.",
            },
        ],
        "exposes": [
            {
                "exposeId": "subscriber_health_360",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "runtime/lineage-sim/subscriber_health_360.parquet"},
                },
                "contract": {"schema": []},
            }
        ],
    }


def _sample_odps_consumes_with_source_system_contract():
    contract = _sample_odps_consumes_contract()
    contract["consumes"][0]["sourceSystem"] = "bss-crm"
    return contract


def test_apply_dry_run_defaults_to_odps_spec():
    """The default ``dataProductSpecification`` is ``odps`` (ODPS-only).

    Pre-2026-05 the default was DPS ``0.0.1`` but Entropy / Data Mesh
    Manager has migrated to ODPS-only on the server side and rejects
    DPS payloads with HTTP 400. Catalog-provider path
    (``fluid publish --target datamesh-manager``) auto-falls-back to
    ODPS via ``_should_retry_with_odps``; the direct CLI path
    (``fluid datamesh-manager publish``) used to fail by default.
    Switching the default here brings both surfaces into line.
    """
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(_sample_contract(), dry_run=True)

    payload = result["payload"]
    # ODPS payload shape: ``apiVersion: v1.0.0`` + ``kind: DataProduct``,
    # no top-level ``dataProductSpecification`` field, no ``info`` block.
    assert payload["apiVersion"] == "v1.0.0"
    assert payload["kind"] == "DataProduct"
    assert "info" not in payload


def test_apply_dry_run_uses_dps_spec_when_explicitly_requested():
    """Legacy DPS path is still reachable via explicit
    ``data_product_specification='0.0.1'`` for any out-of-tree caller
    that still authors against the older spec."""
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(
        _sample_contract(),
        dry_run=True,
        data_product_specification="0.0.1",
    )

    assert result["payload"]["dataProductSpecification"] == "0.0.1"


def test_apply_dry_run_uses_dps_spec_when_provider_hint_is_dps():
    """``provider_hint='dps'`` is the symmetric inverse of the existing
    ``provider_hint='odps'`` form — selects the legacy DPS shape."""
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(_sample_contract(), dry_run=True, provider_hint="dps")

    assert result["payload"]["dataProductSpecification"] == "0.0.1"


def test_apply_dry_run_uses_odps_spec_when_provider_hint_is_odps():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(_sample_contract(), dry_run=True, provider_hint="odps")

    payload = result["payload"]
    assert payload["kind"] == "DataProduct"
    assert "apiVersion" in payload
    assert "info" not in payload


def test_apply_dry_run_allows_explicit_spec_override():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(
        _sample_contract(),
        dry_run=True,
        provider_hint="odps",
        data_product_specification="4.1.0",
    )

    assert result["payload"]["dataProductSpecification"] == "4.1.0"


def test_apply_dry_run_keeps_per_expose_data_contract_ids_for_dps():
    """Per-expose ``dataContractId`` is set on output ports under the
    legacy DPS shape — exercised here via explicit
    ``provider_hint='dps'`` since the default is now ODPS."""
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(
        _sample_contract_with_exposes(),
        dry_run=True,
        publish_contract=True,
        provider_hint="dps",
    )

    output_ports = result["payload"].get("outputPorts", [])
    data_contract_ids = [port.get("dataContractId") for port in output_ports]
    assert data_contract_ids == ["sales-product.orders", "sales-product.customers"]


def test_apply_dry_run_sets_per_expose_contract_ids_for_odps():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(
        _sample_contract_with_exposes(),
        dry_run=True,
        publish_contract=True,
        provider_hint="odps",
    )

    output_ports = result["payload"].get("outputPorts", [])
    contract_ids = [port.get("contractId") for port in output_ports]
    assert contract_ids == ["sales-product.orders", "sales-product.customers"]


def test_apply_dry_run_sets_odps_output_port_display_names_for_dmm():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")
    contract = _sample_contract_with_exposes()
    contract["exposes"][0]["title"] = "Orders"
    contract["exposes"][1]["title"] = "Customers"

    result = provider.apply(
        contract,
        dry_run=True,
        publish_contract=True,
        provider_hint="odps",
    )

    output_ports = result["payload"].get("outputPorts", [])
    assert "id" not in output_ports[0]
    display_names = {
        port["name"]: next(
            prop["value"]
            for prop in port.get("customProperties", [])
            if prop.get("property") == "displayName"
        )
        for port in output_ports
    }
    assert display_names == {"orders": "Orders", "customers": "Customers"}


def test_apply_dry_run_odps_includes_top_level_tags_when_metadata_missing():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(
        _sample_odps_alias_contract(),
        dry_run=True,
        provider_hint="odps",
    )

    payload = result["payload"]
    assert payload["tags"] == ["sales", "gold"]


def test_apply_dry_run_odps_maps_metadata_type_to_custom_property_type():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(
        _sample_odps_alias_contract(),
        dry_run=True,
        provider_hint="odps",
    )

    custom_props = result["payload"].get("customProperties", [])
    assert {"property": "type", "value": "analytical"} in custom_props


def test_apply_dry_run_odps_sets_output_port_type_from_binding_platform():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(
        _sample_odps_binding_platform_contract(),
        dry_run=True,
        provider_hint="odps",
    )

    output_ports = result["payload"].get("outputPorts", [])
    assert output_ports[0]["type"] == "bigquery"


def test_apply_dry_run_odps_maps_product_consumes_to_access_not_input_ports():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(
        _sample_odps_consumes_contract(),
        dry_run=True,
        provider_hint="odps",
    )

    # Product-to-product consumes are Entropy Access agreements, not ODPS
    # inputPorts. Keeping them as inputPorts forces the local CE importer to
    # mirror upstream products as SourceSystems, which renders duplicate graph
    # nodes next to the real product lineage.
    assert "inputPorts" not in result["payload"]
    assert len(result["access_agreements"]) == 2


def test_build_access_agreements_maps_consumes_to_entropy_lineage_edges():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    payloads = provider._build_access_agreements(
        _sample_odps_consumes_contract(),
        "bizlab.teleforge.subscriber_health_360_lineage_local",
        start_date="2026-04-24",
    )

    assert payloads == [
        {
            "id": (
                "bizlab.teleforge.subscriber_health_360_lineage_local__uses__"
                "bizlab.teleforge.subscriber_usage_daily_lineage_local__subscriber_usage_daily"
            ),
            "info": {
                "purpose": "Supply daily subscriber usage features to the health model.",
                "startDate": "2026-04-24",
            },
            "provider": {
                "dataProductId": "bizlab.teleforge.subscriber_usage_daily_lineage_local",
                "outputPortId": "subscriber_usage_daily",
            },
            "consumer": {
                "dataProductId": "bizlab.teleforge.subscriber_health_360_lineage_local",
            },
            "tags": ["fluid", "lineage"],
            "custom": {
                "managedBy": "forge-cli",
                "source": "fluid.consumes",
                "providerContractId": (
                    "bizlab.teleforge.subscriber_usage_daily_lineage_local.subscriber_usage_daily"
                ),
            },
        },
        {
            "id": (
                "bizlab.teleforge.subscriber_health_360_lineage_local__uses__"
                "bizlab.teleforge.billing_health_daily_lineage_local__billing_health_daily"
            ),
            "info": {
                "purpose": "Supply payment behavior and overdue indicators to the health model.",
                "startDate": "2026-04-24",
            },
            "provider": {
                "dataProductId": "bizlab.teleforge.billing_health_daily_lineage_local",
                "outputPortId": "billing_health_daily",
            },
            "consumer": {
                "dataProductId": "bizlab.teleforge.subscriber_health_360_lineage_local",
            },
            "tags": ["fluid", "lineage"],
            "custom": {
                "managedBy": "forge-cli",
                "source": "fluid.consumes",
                "providerContractId": (
                    "bizlab.teleforge.billing_health_daily_lineage_local.billing_health_daily"
                ),
            },
        },
    ]


def test_apply_dry_run_previews_access_agreements_for_consumes():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(
        _sample_odps_consumes_contract(),
        dry_run=True,
        provider_hint="odps",
    )

    access_previews = result["access_agreements"]
    assert len(access_previews) == 2
    first = access_previews[0]
    assert first["method"] == "PUT"
    assert first["url"].endswith(
        "/api/access/"
        "bizlab.teleforge.subscriber_health_360_lineage_local__uses__"
        "bizlab.teleforge.subscriber_usage_daily_lineage_local__subscriber_usage_daily"
    )
    assert first["auto_approve"] is False
    assert "approve_url" not in first
    assert first["payload"]["provider"] == {
        "dataProductId": "bizlab.teleforge.subscriber_usage_daily_lineage_local",
        "outputPortId": "subscriber_usage_daily",
    }
    assert first["payload"]["consumer"] == {
        "dataProductId": "bizlab.teleforge.subscriber_health_360_lineage_local",
    }


def test_apply_publishes_access_agreements_without_approval_by_default():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")
    response = MagicMock(status_code=200)

    with (
        patch.object(provider, "_ensure_team"),
        patch.object(provider, "_ensure_source_systems"),
        patch.object(provider, "_request", return_value=response) as request,
    ):
        result = provider.apply(_sample_odps_consumes_contract(), provider_hint="odps")

    paths = [call.args[1] for call in request.call_args_list]
    assert (
        "/api/access/"
        "bizlab.teleforge.subscriber_health_360_lineage_local__uses__"
        "bizlab.teleforge.subscriber_usage_daily_lineage_local__subscriber_usage_daily"
    ) in paths
    assert not any(path.endswith("/approve") for path in paths)
    assert result["access_agreements"][0]["success"] is True
    assert result["access_agreements"][0]["auto_approved"] is False


def test_apply_publishes_and_approves_access_agreements_when_explicit():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")
    response = MagicMock(status_code=200)

    with (
        patch.object(provider, "_ensure_team"),
        patch.object(provider, "_ensure_source_systems"),
        patch.object(provider, "_request", return_value=response) as request,
    ):
        result = provider.apply(
            _sample_odps_consumes_contract(),
            provider_hint="odps",
            auto_approve_access=True,
        )

    paths = [call.args[1] for call in request.call_args_list]
    assert (
        "/api/access/"
        "bizlab.teleforge.subscriber_health_360_lineage_local__uses__"
        "bizlab.teleforge.subscriber_usage_daily_lineage_local__subscriber_usage_daily"
        "/approve"
    ) in paths
    assert result["access_agreements"][0]["success"] is True
    assert result["access_agreements"][0]["auto_approved"] is True


def test_apply_dry_run_odps_source_system_mode_does_not_restore_product_input_ports():
    """Per Entropy's canonical ``dataproduct-0.0.1.json`` schema, inputPorts
    represent source-system upstreams and require ``sourceSystemId``.
    Product-to-product lineage flows through Access agreements regardless
    of ``odps_lineage_mode`` — putting product references back on
    inputPorts in source-system mode would double-count upstreams in the
    Entropy graph (one Access edge + one phantom SourceSystem node)."""
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(
        _sample_odps_consumes_contract(),
        dry_run=True,
        provider_hint="odps",
        odps_lineage_mode="source-system",
    )

    assert result["odps_lineage_mode"] == "source-system"
    assert "inputPorts" not in result["payload"]
    assert len(result["access_agreements"]) == 2


def test_apply_odps_default_lineage_mode_does_not_upsert_product_references():
    """SourceSystem entities are reserved for explicit
    ``consumes[].sourceSystem`` fields — never invented from product
    references. This locks in the canonical Entropy model where inputPorts
    + SourceSystems represent external systems, while data-product-to-
    data-product lineage flows through Access agreements."""
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")
    response = MagicMock(status_code=200)

    with (
        patch.object(provider, "_ensure_team"),
        patch.object(provider, "_ensure_source_systems") as ensure_sources,
        patch.object(provider, "_request", return_value=response),
    ):
        provider.apply(_sample_odps_consumes_contract(), provider_hint="odps")

    ensure_sources.assert_called_once_with(
        _sample_odps_consumes_contract(),
        "bizlab",
    )


def test_apply_odps_source_system_mode_does_not_upsert_product_references():
    """Same invariant as the default mode: source-system mode does not
    elevate product references into SourceSystem entities. Doing so would
    duplicate upstream nodes in the Entropy graph next to the real Access
    edges."""
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")
    response = MagicMock(status_code=200)

    with (
        patch.object(provider, "_ensure_team"),
        patch.object(provider, "_ensure_source_systems") as ensure_sources,
        patch.object(provider, "_request", return_value=response),
    ):
        provider.apply(
            _sample_odps_consumes_contract(),
            provider_hint="odps",
            odps_lineage_mode="source-system",
        )

    ensure_sources.assert_called_once_with(
        _sample_odps_consumes_contract(),
        "bizlab",
    )


def test_apply_dry_run_odps_promotes_only_retained_source_system_input_ports():
    """Product consumes are Access-only; explicit source-system ports remain."""
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(
        _sample_odps_consumes_with_source_system_contract(),
        dry_run=True,
        provider_hint="odps",
    )

    input_ports = result["payload"].get("inputPorts", [])
    contract_ids = {port["name"]: port["contractId"] for port in input_ports}

    assert contract_ids == {
        "subscriber_usage_daily": (
            "bizlab.teleforge.subscriber_usage_daily_lineage_local.subscriber_usage_daily"
        )
    }


def test_apply_dry_run_odps_product_consume_with_explicit_contract_id_is_access_only():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    contract = _sample_odps_consumes_contract()
    contract["consumes"][0][
        "contractId"
    ] = "bizlab.teleforge.subscriber_usage_daily_lineage_local.custom_view"

    result = provider.apply(contract, dry_run=True, provider_hint="odps")

    assert "inputPorts" not in result["payload"]
    assert result["access_agreements"][0]["payload"]["custom"]["providerContractId"] == (
        "bizlab.teleforge.subscriber_usage_daily_lineage_local.custom_view"
    )


def test_apply_dry_run_odps_preserves_input_port_source_system_metadata():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    result = provider.apply(
        _sample_odps_consumes_with_source_system_contract(),
        dry_run=True,
        provider_hint="odps",
    )

    input_ports = result["payload"].get("inputPorts", [])
    props = input_ports[0].get("customProperties") or []
    source_system = next(
        (p.get("value") for p in props if p.get("property") == "sourceSystem"),
        None,
    )
    assert source_system == "bss-crm"


def test_cmd_publish_passes_provider_hint_to_apply():
    # Use a contract that actually conforms to fluid-schema-0.7.2.json so
    # the master-schema validation step in ``_cmd_publish`` (run in strict
    # mode here) does not abort before ``provider.apply`` is called. This
    # test's intent is to verify CLI-flag forwarding to ``provider.apply``,
    # not to re-test contract validity.
    valid_contract = {
        "fluidVersion": "0.7.2",
        "kind": "DataProduct",
        "id": "sales.product",
        "name": "Sales Product",
        "description": "demo",
        "metadata": {
            "layer": "Gold",
            "owner": {"team": "analytics"},
        },
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "data/orders.parquet"},
                },
                "contract": {"schema": []},
            }
        ],
    }

    args = SimpleNamespace(
        contract="contract.fluid.yaml",
        overlay=None,
        dry_run=True,
        team_id=None,
        no_create_team=False,
        with_contract=False,
        contract_format="odcs",
        data_product_spec=None,
        validate_generated_contracts=True,
        validation_mode="strict",
        fail_on_contract_error=False,
        provider="odps",
    )

    mock_provider = MagicMock()
    mock_provider.apply.return_value = {
        "dry_run": True,
        "method": "PUT",
        "url": "https://api.entropy-data.com/api/dataproducts/sales.product",
        "payload": {"id": "sales.product", "kind": "DataProduct", "apiVersion": "v1.0.0"},
    }

    with patch(
        "fluid_build.cli.datamesh_manager.load_contract_with_overlay",
        return_value=valid_contract,
    ):
        with patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider):
            with patch("fluid_build.cli.datamesh_manager._print_dry_run"):
                code = _cmd_publish(args)

    assert code == 0
    _, kwargs = mock_provider.apply.call_args
    assert kwargs["provider_hint"] == "odps"
    assert kwargs["data_product_specification"] is None
    assert kwargs["validate_generated_contracts"] is True
    assert kwargs["validation_mode"] == "strict"


def test_cmd_publish_fail_on_contract_error_returns_non_zero():
    args = SimpleNamespace(
        contract="contract.fluid.yaml",
        overlay=None,
        dry_run=False,
        team_id=None,
        no_create_team=False,
        with_contract=True,
        contract_format="odcs",
        data_product_spec=None,
        validate_generated_contracts=False,
        validation_mode="warn",
        fail_on_contract_error=True,
        provider="odps",
    )

    mock_provider = MagicMock()
    mock_provider.apply.return_value = {
        "success": True,
        "product_id": "sales-product",
        "odcs_contracts": [
            {"contract_id": "sales-product.a", "success": True},
            {"contract_id": "sales-product.b", "success": False, "error": "boom"},
        ],
    }

    with patch(
        "fluid_build.cli.datamesh_manager.load_contract_with_overlay",
        return_value=_sample_contract(),
    ):
        with patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider):
            with patch("fluid_build.cli.datamesh_manager._print_publish_result"):
                code = _cmd_publish(args)

    assert code == 1


def test_request_wraps_retry_error_as_provider_error():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")
    session = MagicMock()
    session.request.side_effect = requests.exceptions.RetryError("too many 500 responses")
    provider._session_instance = session

    with pytest.raises(ProviderError) as excinfo:
        provider._request("PUT", "/api/datacontracts/x.y", json_body={"id": "x.y"})

    assert "HTTP request failed" in str(excinfo.value)


def test_provider_rejects_plain_http_remote_api_url():
    with pytest.raises(ProviderError) as excinfo:
        DataMeshManagerProvider(api_key="dummy", api_url="http://catalog.example.com")

    assert "plain HTTP to a non-local host" in str(excinfo.value)


def test_request_redacts_secret_like_error_body():
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")
    response = MagicMock(status_code=403, text='{"api_key":"ed_live_supersecret"}')
    session = MagicMock()
    session.request.return_value = response
    provider._session_instance = session

    with pytest.raises(ProviderError) as excinfo:
        provider._request("PUT", "/api/dataproducts/x", json_body={"id": "x"})

    message = str(excinfo.value)
    assert "ed_live_supersecret" not in message
    assert "***REDACTED***" in message


def test_publish_exit_code_strict_mode_on_invalid_contract():
    args = SimpleNamespace(validation_mode="strict", fail_on_contract_error=False)
    result = {
        "odcs_contracts": [
            {"contract_id": "a", "success": True, "valid": True},
            {"contract_id": "b", "success": True, "valid": False},
        ]
    }

    assert _publish_exit_code(result, args) == 1


def test_publish_exit_code_fail_on_contract_error():
    args = SimpleNamespace(validation_mode="warn", fail_on_contract_error=True)
    result = {
        "odcs_contracts": [
            {"contract_id": "a", "success": True},
            {"contract_id": "b", "success": False, "error": "boom"},
        ]
    }

    assert _publish_exit_code(result, args) == 1


def test_publish_odcs_strict_validation_skips_put_on_invalid(monkeypatch):
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")
    contract = {
        "id": "sales-product",
        "metadata": {"name": "Sales Product"},
        "owner": {"team": "analytics"},
        "exposes": [{"id": "port_a"}],
        "expects": [],
    }

    class _FakeOdcsProvider:
        def render(self, fluid, expose_id=None):
            return {"id": f"sales-product.{expose_id}", "kind": "DataContract"}

    monkeypatch.setattr(
        "fluid_build.providers.odcs.OdcsProvider",
        _FakeOdcsProvider,
        raising=True,
    )
    monkeypatch.setattr(
        provider,
        "_validate_generated_odcs_contract",
        lambda _odcs_provider, _odcs_body: (False, "ODCS validation failed"),
    )

    request_calls = []
    monkeypatch.setattr(
        provider,
        "_request",
        lambda *args, **kwargs: request_calls.append((args, kwargs)),
    )

    results = provider._publish_odcs_per_expose(
        contract,
        "sales-product",
        validate_generated_contracts=True,
        validation_mode="strict",
    )

    assert request_calls == []
    assert results[0]["success"] is False
    assert results[0]["valid"] is False
    assert results[0]["error_type"] == "VALIDATION_FAILED"


def test_publish_odcs_warn_validation_still_puts(monkeypatch):
    provider = DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")
    contract = {
        "id": "sales-product",
        "metadata": {"name": "Sales Product"},
        "owner": {"team": "analytics"},
        "exposes": [{"id": "port_a"}],
        "expects": [],
    }

    class _FakeOdcsProvider:
        def render(self, fluid, expose_id=None):
            return {"id": f"sales-product.{expose_id}", "kind": "DataContract"}

    monkeypatch.setattr(
        "fluid_build.providers.odcs.OdcsProvider",
        _FakeOdcsProvider,
        raising=True,
    )
    monkeypatch.setattr(
        provider,
        "_validate_generated_odcs_contract",
        lambda _odcs_provider, _odcs_body: (False, "ODCS validation failed"),
    )

    class _Resp:
        status_code = 200

    request_calls = []
    monkeypatch.setattr(
        provider,
        "_request",
        lambda *args, **kwargs: request_calls.append((args, kwargs)) or _Resp(),
    )

    results = provider._publish_odcs_per_expose(
        contract,
        "sales-product",
        validate_generated_contracts=True,
        validation_mode="warn",
    )

    assert len(request_calls) == 1
    assert results[0]["success"] is True
    assert results[0]["valid"] is False
    assert "validation_error" in results[0]
    assert "schema_objects" in results[0]
    assert "schema_properties" in results[0]
