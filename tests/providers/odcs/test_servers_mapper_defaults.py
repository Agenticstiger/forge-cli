# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""ODCS server-mapper required-field defaulting.

Live forge-AI runs against Gemini Flash routinely emit Snowflake
bindings with only ``database`` + ``schema`` set — missing
``account`` which the ODCS v3.1.0 ``SnowflakeServer`` schema requires.
Without a default, every such forge-AI generation produced an invalid
ODCS export.

Fix: ``_server_details_from_location`` synthesises ``${SNOWFLAKE_*}``
env-var placeholders for the three required-by-ODCS fields when the
binding omits them. Deploy-time substitution fills the real values.
"""

from __future__ import annotations

import pytest

from fluid_build.providers.odcs.mappers.servers import _server_details_from_location


class TestSnowflakeRequiredFieldDefaults:
    """ODCS Snowflake: account + database + schema are required."""

    def test_empty_location_defaults_all_three(self) -> None:
        details = _server_details_from_location({}, "snowflake")
        assert details["account"] == "${SNOWFLAKE_ACCOUNT}"
        assert details["database"] == "${SNOWFLAKE_DATABASE}"
        assert details["schema"] == "${SNOWFLAKE_SCHEMA}"

    def test_explicit_values_win(self) -> None:
        """User-provided values must NOT be overwritten by the defaults."""
        details = _server_details_from_location(
            {
                "account": "acme-prod",
                "database": "ANALYTICS",
                "schema": "CUSTOMER_360",
            },
            "snowflake",
        )
        assert details["account"] == "acme-prod"
        assert details["database"] == "ANALYTICS"
        assert details["schema"] == "CUSTOMER_360"

    def test_partial_explicit_fills_only_gaps(self) -> None:
        """Mixed case — user gave database+schema but not account
        (the actual LLM-output shape that triggered this fix)."""
        details = _server_details_from_location(
            {"database": "TELCO_DB", "schema": "SUBSCRIBER_360"},
            "snowflake",
        )
        # Real value preserved
        assert details["database"] == "TELCO_DB"
        assert details["schema"] == "SUBSCRIBER_360"
        # Missing required field synthesised
        assert details["account"] == "${SNOWFLAKE_ACCOUNT}"

    def test_optional_fields_not_synthesised(self) -> None:
        """host / port / warehouse are NOT required by ODCS Snowflake
        and must remain absent when not provided — defaulting them
        would pollute every contract with unwanted fields."""
        details = _server_details_from_location({}, "snowflake")
        assert "host" not in details
        assert "port" not in details
        assert "warehouse" not in details

    def test_snowflake_provider_alias(self) -> None:
        """The mapper accepts lowercase ``snowflake`` (the
        canonical form in FLUID bindings) — this pins the
        case-insensitive lookup."""
        details = _server_details_from_location({"warehouse": "WH_X"}, "SNOWFLAKE")
        assert details["warehouse"] == "WH_X"
        assert details["account"] == "${SNOWFLAKE_ACCOUNT}"


class TestSnowflakeRoundTripValidates:
    """End-to-end pin: a minimal FLUID contract with a Snowflake binding
    that omits ``account`` (the LLM-emitted shape) must produce an ODCS
    that passes vendored-schema validation."""

    def test_minimal_binding_renders_valid_odcs(self) -> None:
        from fluid_build.providers.odcs import OdcsProvider
        from fluid_build.providers.odcs.validation import collect_errors, load_schema

        fluid = {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": "telco.customer_360",
            "name": "Customer 360",
            "domain": "telco",
            "metadata": {"layer": "Silver", "productType": "ADP"},
            "exposes": [
                {
                    "exposeId": "customer_360_view",
                    "kind": "view",
                    "binding": {
                        "platform": "snowflake",
                        "format": "snowflake_table",
                        "location": {
                            "database": "ANALYTICS",
                            "schema": "CUSTOMER_360",
                            # Note: NO account — LLM-output shape
                        },
                    },
                    "contract": {
                        "schema": [
                            {"name": "customer_id", "type": "string", "required": True},
                            {"name": "lifetime_value", "type": "double"},
                        ],
                    },
                }
            ],
        }
        odcs = OdcsProvider().render(fluid)
        schema = load_schema()
        if schema is None:
            pytest.skip("vendored ODCS schema not available")
        errors = collect_errors(odcs, schema)
        # Filter to the original bug: servers.*.account / database / schema
        server_field_errors = [
            e
            for e in errors
            if "servers" in e.get("path", "")
            and any(req in e.get("message", "") for req in ("account", "database", "schema"))
        ]
        assert not server_field_errors, (
            f"Snowflake server still missing required ODCS fields after default-fill: "
            f"{server_field_errors}"
        )
