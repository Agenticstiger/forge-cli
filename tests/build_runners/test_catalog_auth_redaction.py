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

"""Every Iceberg sink message goes through the shared secret redactor.

The checks quote contract values. A catalog ``uri`` or an override warehouse
with a ``user:password@`` part, or the value of the variable a catalogAuth
names, must not reach ``fluid validate``'s output, the run preflight's log or
the run error.
"""

from __future__ import annotations

import logging

import pytest

from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
    validate_iceberg_sink,
)
from fluid_build.observability.secret_redactor import forget_known_secrets, redact_secret_text
from tests.build_runners.test_lakekeeper_warehouse_reaching import _binding, _contract

pytestmark = [pytest.mark.unit]

_PASSWORD = "hunter2-not-a-real-pass"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _no_registered_secrets():
    forget_known_secrets()
    yield
    forget_known_secrets()


def _uri_contract():
    binding = _binding(
        "lakekeeper",
        uri=f"http://admin:{_PASSWORD}@lakekeeper:8181",  # no /catalog: draws a warning
        warehouse="analytics",
    )
    return _contract(binding)


def test_a_catalog_uri_password_is_masked_in_validate():
    errors, warnings = validate_iceberg_sink(_uri_contract())
    joined = "\n".join(errors + warnings)
    assert "does not end in '/catalog'" in joined, joined
    assert _PASSWORD not in joined
    assert "***REDACTED***" in joined


def test_a_catalog_uri_password_is_masked_in_the_preflight_log(caplog):
    with caplog.at_level(logging.WARNING):
        iceberg_sink_preflight(_uri_contract(), "ingest")
    assert "does not end in '/catalog'" in caplog.text, caplog.text
    assert _PASSWORD not in caplog.text


def test_an_override_warehouse_password_is_masked():
    kc = {"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": f"s3://u:{_PASSWORD}@b/wh"}}
    binding = _binding("lakekeeper", uri="http://c:8181/catalog", warehouse="analytics")
    errors, _warnings = validate_iceberg_sink(_contract(binding, kc=kc))
    joined = "\n".join(errors)
    assert "addresses a warehouse by NAME" in joined, joined
    assert _PASSWORD not in joined


def test_the_embedded_preflight_registers_the_variable_value(monkeypatch):
    monkeypatch.setenv("FW_LK_CREDENTIAL", f"spark:{_PASSWORD}")
    binding = _binding("lakekeeper", uri="http://c:8181/catalog", warehouse="analytics")
    contract = _contract(
        binding, debezium={"deployment": {"mode": "embedded"}, "server": {"sink": {}}}
    )
    contract["builds"][0]["properties"]["sink"]["catalogAuth"] = {
        "type": "oauth2",
        "credentialEnv": "FW_LK_CREDENTIAL",
        "oauth2ServerUri": "https://kc.example/realms/x/protocol/openid-connect/token",
        "scope": "lakekeeper",
    }
    assert iceberg_sink_preflight(contract, "ingest") is None
    assert _PASSWORD not in redact_secret_text(f"anything spark:{_PASSWORD} anything")
