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

"""Tests for the three DMM provider gap fixes:

Gap 1: publish_test_results — POST /api/test-results
Gap 2: Enhanced _publish_data_contract_internal mapping
Gap 3: Fixed _resolve_location + _extract_provider for 0.7.1 bindings
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.providers.datamesh_manager.datamesh_manager import (
    DataMeshManagerProvider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(**kwargs):
    """Create a DataMeshManagerProvider with a fake API key."""
    return DataMeshManagerProvider(api_key="test-key-123", **kwargs)


def _make_report(
    *,
    is_valid=True,
    issues=None,
    contract_id="test-product",
    contract_version="1.0.0",
    duration=1.23,
    checks_passed=5,
    checks_failed=0,
):
    """Create a mock ValidationReport."""
    report = MagicMock()
    report.contract_id = contract_id
    report.contract_version = contract_version
    report.is_valid.return_value = is_valid
    report.duration = duration
    report.validation_time = datetime(2025, 1, 15, 12, 0, 0)
    report.checks_passed = checks_passed
    report.checks_failed = checks_failed
    report.issues = issues or []
    return report


def _make_issue(
    severity="error", category="schema", message="bad field", path="exposes[0].schema.fields[0]"
):
    issue = MagicMock()
    issue.severity = severity
    issue.category = category
    issue.message = message
    issue.path = path
    return issue


# ===================================================================
# Gap 3: _extract_provider + _resolve_location (FLUID 0.7.1)
# ===================================================================


class TestExtractProvider:
    """Test _extract_provider with legacy and 0.7.1 patterns."""

    def test_binding_platform(self):
        section = {"binding": {"platform": "gcp", "format": "bigquery_table"}}
        assert DataMeshManagerProvider._extract_provider(section) == "gcp"

    def test_legacy_provider(self):
        section = {"provider": "snowflake"}
        assert DataMeshManagerProvider._extract_provider(section) == "snowflake"

    def test_binding_takes_precedence(self):
        section = {"provider": "old", "binding": {"platform": "gcp"}}
        assert DataMeshManagerProvider._extract_provider(section) == "gcp"

    def test_empty(self):
        assert DataMeshManagerProvider._extract_provider({}) == ""


class TestResolveLocationV071:
    """Test _resolve_location with FLUID 0.7.1 binding.location pattern."""

    def test_gcp_binding_location(self):
        section = {
            "binding": {
                "platform": "gcp",
                "location": {
                    "project": "my-project",
                    "dataset": "my_dataset",
                    "table": "my_table",
                    "region": "EU",
                },
            }
        }
        result = DataMeshManagerProvider._resolve_location(section, "gcp")
        assert result == "my-project.my_dataset.my_table"

    def test_snowflake_binding_location(self):
        section = {
            "binding": {
                "platform": "snowflake",
                "location": {
                    "account": "{{ env.SNOWFLAKE_ACCOUNT }}",
                    "database": "CRYPTO_DATA",
                    "schema": "MARKET_DATA",
                    "table": "BITCOIN_PRICES",
                },
            }
        }
        result = DataMeshManagerProvider._resolve_location(section, "snowflake")
        assert result == "CRYPTO_DATA.MARKET_DATA.BITCOIN_PRICES"

    def test_aws_binding_location_with_bucket(self):
        section = {
            "binding": {
                "platform": "aws",
                "location": {
                    "database": "crypto_data",
                    "table": "bitcoin_prices",
                    "bucket": "my-bucket",
                    "path": "data/bitcoin/prices/",
                    "region": "eu-central-1",
                },
            }
        }
        result = DataMeshManagerProvider._resolve_location(section, "aws")
        assert result == "s3://my-bucket/data/bitcoin/prices"

    def test_aws_binding_location_glue_only(self):
        section = {
            "binding": {
                "platform": "aws",
                "location": {
                    "database": "crypto_data",
                    "table": "bitcoin_prices",
                },
            }
        }
        result = DataMeshManagerProvider._resolve_location(section, "aws")
        assert result == "crypto_data.bitcoin_prices"

    def test_legacy_gcp_still_works(self):
        section = {
            "gcp": {"project": "old-project", "dataset": "ds", "table": "tbl"},
        }
        result = DataMeshManagerProvider._resolve_location(section, "gcp")
        assert result == "old-project.ds.tbl"

    def test_legacy_snowflake_still_works(self):
        section = {
            "snowflake": {"database": "DB", "schema": "SCH", "table": "TBL"},
        }
        result = DataMeshManagerProvider._resolve_location(section, "snowflake")
        assert result == "DB.SCH.TBL"

    def test_legacy_aws_s3_still_works(self):
        section = {
            "aws": {"bucket": "bkt", "prefix": "pfx"},
        }
        result = DataMeshManagerProvider._resolve_location(section, "aws")
        assert result == "s3://bkt/pfx"

    def test_redshift_binding_location(self):
        section = {
            "binding": {
                "platform": "redshift",
                "location": {"database": "db", "schema": "pub", "table": "events"},
            }
        }
        result = DataMeshManagerProvider._resolve_location(section, "redshift")
        assert result == "db.pub.events"

    def test_kafka_binding_location(self):
        section = {
            "binding": {
                "platform": "kafka",
                "location": {"topic": "events.v1"},
            }
        }
        result = DataMeshManagerProvider._resolve_location(section, "kafka")
        assert result == "events.v1"

    def test_fallback_location_field(self):
        section = {"location": "custom://my-location"}
        result = DataMeshManagerProvider._resolve_location(section, "")
        assert result == "custom://my-location"

    def test_empty_section(self):
        result = DataMeshManagerProvider._resolve_location({}, "")
        assert result == ""


class TestMapOutputPortsV071:
    """Test that _map_output_ports correctly uses exposeId and binding.platform."""

    def test_uses_expose_id_and_title(self):
        provider = _make_provider()
        fluid = {
            "exposes": [
                {
                    "exposeId": "btc_table",
                    "title": "Bitcoin Prices",
                    "description": "BTC data",
                    "binding": {
                        "platform": "gcp",
                        "location": {"project": "p", "dataset": "d", "table": "t"},
                    },
                }
            ],
        }
        ports = provider._map_output_ports(fluid)
        assert len(ports) == 1
        assert ports[0]["id"] == "btc_table"
        assert ports[0]["name"] == "Bitcoin Prices"
        assert ports[0]["type"] == "BigQuery"
        # After DPS 0.0.1 refactor, output ports use structured server objects
        # GCP binding maps: project→account, dataset→database, table→table
        server = ports[0]["server"]
        assert server["account"] == "p"
        assert server["database"] == "d"
        assert server["table"] == "t"


# ===================================================================
# Gap 2: Enhanced _publish_data_contract_internal
# ===================================================================


class TestPublishDataContractInternal:
    """Test the enhanced data contract mapping."""

    def _fluid_contract(self):
        return {
            "id": "test-product",
            "name": "Test Product",
            "description": "A test data product",
            "domain": "finance",
            "metadata": {
                "name": "Test Product",
                "version": "2.0.0",
                "description": "A test data product",
                "owner": {"team": "data-eng"},
            },
            "tags": ["crypto", "real-time"],
            "labels": {"cost_center": "CC-123"},
            "sla": {
                "freshness": "PT1H",
                "availability": "99.9%",
                "completeness": "99%",
            },
            "builds": {
                "runtime": "python3.11",
                "schedule": "*/5 * * * *",
            },
            "exposes": [
                {
                    "exposeId": "btc_prices",
                    "title": "Bitcoin Prices",
                    "kind": "table",
                    "binding": {
                        "platform": "gcp",
                        "format": "bigquery_table",
                        "location": {
                            "project": "my-proj",
                            "dataset": "crypto",
                            "table": "btc_prices",
                            "region": "EU",
                        },
                    },
                    "policy": {
                        "dq": {
                            "rules": [
                                {
                                    "id": "freshness_check",
                                    "type": "freshness",
                                    "severity": "critical",
                                    "window": "PT6H",
                                    "description": "Data must be fresh",
                                },
                                {
                                    "id": "completeness_price",
                                    "type": "completeness",
                                    "severity": "error",
                                    "selector": "price_usd",
                                    "threshold": 0.95,
                                    "description": "95% price completeness",
                                },
                            ]
                        }
                    },
                    "contract": {
                        "schema": [
                            {
                                "name": "price_usd",
                                "type": "numeric",
                                "required": True,
                                "description": "BTC price",
                                "sensitivity": "cleartext",
                            },
                            {
                                "name": "timestamp",
                                "type": "timestamp",
                                "required": True,
                                "description": "Record time",
                            },
                        ]
                    },
                }
            ],
        }

    @patch.object(DataMeshManagerProvider, "_request")
    def test_includes_domain(self, mock_req):
        mock_req.return_value = MagicMock(status_code=200)
        provider = _make_provider()
        provider._publish_data_contract_internal(self._fluid_contract(), "test-product", fmt="dcs")
        payload = mock_req.call_args[1]["json_body"]
        assert payload["info"]["domain"] == "finance"

    @patch.object(DataMeshManagerProvider, "_request")
    def test_includes_dq_rules(self, mock_req):
        mock_req.return_value = MagicMock(status_code=200)
        provider = _make_provider()
        provider._publish_data_contract_internal(self._fluid_contract(), "test-product", fmt="dcs")
        payload = mock_req.call_args[1]["json_body"]
        checks = payload["quality"]["checks"]
        assert len(checks) == 2
        assert checks[0]["type"] == "freshness"
        assert checks[0]["window"] == "PT6H"
        assert checks[1]["type"] == "completeness"
        assert checks[1]["field"] == "price_usd"
        assert checks[1]["threshold"] == 0.95

    @patch.object(DataMeshManagerProvider, "_request")
    def test_includes_server_from_binding(self, mock_req):
        mock_req.return_value = MagicMock(status_code=200)
        provider = _make_provider()
        provider._publish_data_contract_internal(self._fluid_contract(), "test-product", fmt="dcs")
        payload = mock_req.call_args[1]["json_body"]
        servers = payload["servers"]
        assert "btc_prices" in servers
        server = servers["btc_prices"]
        assert server["type"] == "BigQuery"
        assert server["project"] == "my-proj"
        assert server["dataset"] == "crypto"

    @patch.object(DataMeshManagerProvider, "_request")
    def test_includes_sla_and_completeness(self, mock_req):
        mock_req.return_value = MagicMock(status_code=200)
        provider = _make_provider()
        provider._publish_data_contract_internal(self._fluid_contract(), "test-product", fmt="dcs")
        payload = mock_req.call_args[1]["json_body"]
        q = payload["quality"]
        assert q["freshness"] == "PT1H"
        assert q["availability"] == "99.9%"
        assert q["completeness"] == "99%"

    @patch.object(DataMeshManagerProvider, "_request")
    def test_includes_builds_as_custom(self, mock_req):
        mock_req.return_value = MagicMock(status_code=200)
        provider = _make_provider()
        provider._publish_data_contract_internal(self._fluid_contract(), "test-product", fmt="dcs")
        payload = mock_req.call_args[1]["json_body"]
        assert payload["custom"]["builds"]["runtime"] == "python3.11"

    @patch.object(DataMeshManagerProvider, "_request")
    def test_includes_tags_and_labels(self, mock_req):
        mock_req.return_value = MagicMock(status_code=200)
        provider = _make_provider()
        provider._publish_data_contract_internal(self._fluid_contract(), "test-product", fmt="dcs")
        payload = mock_req.call_args[1]["json_body"]
        assert "crypto" in payload["custom"]["tags"]
        assert payload["custom"]["labels"]["cost_center"] == "CC-123"

    @patch.object(DataMeshManagerProvider, "_request")
    def test_field_mapping_includes_required_and_sensitivity(self, mock_req):
        mock_req.return_value = MagicMock(status_code=200)
        provider = _make_provider()
        provider._publish_data_contract_internal(self._fluid_contract(), "test-product", fmt="dcs")
        payload = mock_req.call_args[1]["json_body"]
        fields = payload["models"]["btc_prices"]["fields"]
        assert fields["price_usd"]["required"] is True
        assert fields["price_usd"]["classification"] == "cleartext"

    @patch.object(DataMeshManagerProvider, "_request")
    def test_model_uses_kind(self, mock_req):
        mock_req.return_value = MagicMock(status_code=200)
        provider = _make_provider()
        provider._publish_data_contract_internal(self._fluid_contract(), "test-product", fmt="dcs")
        payload = mock_req.call_args[1]["json_body"]
        assert payload["models"]["btc_prices"]["type"] == "table"

    @patch.object(DataMeshManagerProvider, "_request")
    def test_skips_template_vars_in_server(self, mock_req):
        mock_req.return_value = MagicMock(status_code=200)
        provider = _make_provider()
        fluid = {
            "id": "test",
            "metadata": {"owner": {"team": "t"}},
            "exposes": [
                {
                    "exposeId": "tbl",
                    "binding": {
                        "platform": "snowflake",
                        "location": {
                            "account": "{{ env.SF_ACCOUNT }}",
                            "database": "DB",
                            "schema": "SCH",
                            "table": "TBL",
                        },
                    },
                    "contract": {"schema": [{"name": "col1", "type": "string"}]},
                }
            ],
        }
        provider._publish_data_contract_internal(fluid, "test", fmt="dcs")
        payload = mock_req.call_args[1]["json_body"]
        server = payload["servers"]["tbl"]
        assert "account" not in server  # template var excluded
        assert server["database"] == "DB"


# ===================================================================
# Gap 1: publish_test_results
# ===================================================================


class TestPublishTestResults:
    """Test the new publish_test_results method."""

    @patch.object(DataMeshManagerProvider, "_session")
    def test_post_passing_report(self, mock_session):
        mock_resp = MagicMock(status_code=200, text="OK")
        mock_session.return_value.request.return_value = mock_resp

        provider = _make_provider()
        report = _make_report(is_valid=True, checks_passed=5, checks_failed=0)
        result = provider.publish_test_results(report)

        assert result["success"] is True
        assert result["status_code"] == 200

        call_args = mock_session.return_value.request.call_args
        assert call_args[0][0] == "POST"
        assert "/api/test-results" in call_args[0][1]

        payload = call_args[1]["json"]
        assert payload["result"] == "passed"
        assert payload["dataContractId"] == "test-product"
        assert payload["checks"]["passed"] == 5
        assert payload["checks"]["failed"] == 0

    @patch.object(DataMeshManagerProvider, "_session")
    def test_post_failing_report(self, mock_session):
        mock_resp = MagicMock(status_code=200, text="OK")
        mock_session.return_value.request.return_value = mock_resp

        issues = [
            _make_issue(severity="error", category="schema", message="missing field X"),
            _make_issue(severity="warning", category="quality", message="low completeness"),
        ]
        provider = _make_provider()
        report = _make_report(is_valid=False, issues=issues, checks_passed=3, checks_failed=2)
        provider.publish_test_results(report)

        payload = mock_session.return_value.request.call_args[1]["json"]
        assert payload["result"] == "failed"
        assert len(payload["results"]) == 2
        assert payload["results"][0]["result"] == "failed"
        assert payload["results"][1]["result"] == "passed"

    @patch.object(DataMeshManagerProvider, "_session")
    def test_custom_publish_url(self, mock_session):
        mock_resp = MagicMock(status_code=201, text="Created")
        mock_session.return_value.request.return_value = mock_resp

        provider = _make_provider()
        report = _make_report()
        url = "https://api.datamesh-manager.com/api/test-results"
        result = provider.publish_test_results(report, publish_url=url)

        call_url = mock_session.return_value.request.call_args[0][1]
        assert call_url == url
        assert result["url"] == url

    @patch.object(DataMeshManagerProvider, "_session")
    def test_api_error_raises(self, mock_session):
        mock_resp = MagicMock(status_code=401, text="Unauthorized")
        mock_session.return_value.request.return_value = mock_resp

        provider = _make_provider()
        report = _make_report()
        with pytest.raises(Exception, match="401"):
            provider.publish_test_results(report)

    def test_requires_api_key(self):
        provider = DataMeshManagerProvider(api_key="")
        report = _make_report()
        with pytest.raises(Exception, match="DMM_API_KEY"):
            provider.publish_test_results(report)

    @patch.object(DataMeshManagerProvider, "_session")
    def test_empty_issues_produces_single_passed(self, mock_session):
        mock_resp = MagicMock(status_code=200, text="OK")
        mock_session.return_value.request.return_value = mock_resp

        provider = _make_provider()
        report = _make_report(issues=[])
        provider.publish_test_results(report)

        payload = mock_session.return_value.request.call_args[1]["json"]
        assert len(payload["results"]) == 1
        assert payload["results"][0]["result"] == "passed"


# ===================================================================
# Gap 1 CLI integration: --publish flag on fluid test
# ===================================================================


class TestFluidTestPublishFlag:
    """Test that --publish is wired into the fluid test CLI."""

    def test_publish_arg_registered(self):
        """The --publish argument should be registered on the test parser."""
        import argparse

        from fluid_build.cli.test import register

        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        args = parser.parse_args(
            ["test", "contract.yaml", "--publish", "https://api.entropy-data.com/api/test-results"]
        )
        assert args.publish == "https://api.entropy-data.com/api/test-results"

    def test_publish_arg_optional(self):
        """Without --publish the attribute should be None."""
        import argparse

        from fluid_build.cli.test import register

        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        args = parser.parse_args(["test", "contract.yaml"])
        assert args.publish is None
