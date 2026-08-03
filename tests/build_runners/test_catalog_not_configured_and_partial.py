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

"""Honest degradation for catalog publishes (#467).

Two ways a publish told the operator the wrong thing:

* an **unconfigured** target still built a registrar pointed at the
  placeholder ``https://openmetadata.test`` / ``https://datahub.test`` and
  reported ``cannot resolve hostname`` — a DNS diagnostic where the real
  problem was a missing environment variable. ``build_registrar``'s own
  docstring already promised "Returns None when the target's required
  endpoint is unset — the dispatcher then records a clear 'not configured'
  result instead of dialling a placeholder host"; the dataclass default
  defeated it.
* an ODCS Data Contract registration that **failed** was logged at DEBUG and
  the result still said ``succeeded=True, error=None``. The whole point of
  that route is that the ``extension`` blob is invisible to the contracts UI,
  to contract search and to validation runs — so silently falling back to it
  is a partially-applied publish reported as a complete one.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import pytest

from fluid_build.api.catalog_backend import CatalogNotConfiguredError
from fluid_build.api.catalog_publication import CatalogPublicationPayload
from fluid_build.build_runners import _catalog as catalog

pytestmark = pytest.mark.unit


CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "bronze.community.om_probe_v1",
    "name": "OM probe",
    "metadata": {"owner": {"team": "data-platform"}},
    "exposes": [
        {
            "exposeId": "customers_raw",
            "kind": "table",
            "binding": {
                "platform": "local",
                "format": "parquet",
                "location": {"path": "customers.parquet"},
            },
            "contract": {"schema": [{"name": "ID", "type": "int"}]},
        }
    ],
}

_ENDPOINT_ENV = (
    "FLUID_CATALOG_OPENMETADATA_URL",
    "OPENMETADATA_SERVER_URL",
    "OPENMETADATA_HOST",
    "FLUID_CATALOG_DATAHUB_URL",
    "DATAHUB_GMS_URL",
)


@pytest.fixture()
def no_catalog_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENDPOINT_ENV:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# not configured
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["openmetadata", "datahub"])
def test_unconfigured_target_builds_no_registrar(no_catalog_env: None, target: str) -> None:
    assert catalog.build_registrar(target, {}) is None


@pytest.mark.parametrize(
    ("target", "placeholder"),
    [("openmetadata", "openmetadata.test"), ("datahub", "datahub.test")],
)
def test_unconfigured_target_never_points_at_a_placeholder_host(
    no_catalog_env: None, target: str, placeholder: str
) -> None:
    registrar = catalog.build_registrar(target, {})
    assert getattr(registrar, "base_url", "") != f"https://{placeholder}"


def test_unconfigured_publish_names_the_missing_setting(no_catalog_env: None) -> None:
    payload = CatalogPublicationPayload.from_contract(CONTRACT, {})
    plan = catalog.CatalogPlan.from_dict({"register": ["openmetadata"]})
    outcome = catalog.register_all_payload(plan, payload, target_configs={})

    (result,) = outcome.results
    assert result.succeeded is False
    assert "not configured" in result.error
    assert "FLUID_CATALOG_OPENMETADATA_URL" in result.error
    # The old message pointed at DNS, not at configuration.
    assert "resolve hostname" not in result.error


def test_a_configured_endpoint_still_builds(no_catalog_env: None) -> None:
    registrar = catalog.build_registrar("openmetadata", {"endpoint": "http://om.internal:8585"})
    assert registrar is not None
    assert registrar.base_url == "http://om.internal:8585"


def test_not_configured_error_message_is_resolved_from_the_spec() -> None:
    message = CatalogNotConfiguredError("openmetadata").operator_message()
    assert "openmetadata not configured" in message
    assert "FLUID_CATALOG_OPENMETADATA_URL" in message


# ---------------------------------------------------------------------------
# partial publish
# ---------------------------------------------------------------------------


def _registrar(monkeypatch: pytest.MonkeyPatch, *, contract_fails: bool):
    """An OpenMetadata registrar whose table PUT works and whose ODCS Data
    Contract PUT optionally 404s, as a pre-1.10 server does."""
    from fluid_build.build_runners.catalog_registrars.openmetadata import (
        OpenMetadataRegistrar,
    )

    registrar = OpenMetadataRegistrar(base_url="http://om.internal:8585")
    monkeypatch.setattr(registrar, "_put", lambda body: {"id": "ent-123"})
    monkeypatch.setattr(registrar, "_resolve_entity_id", lambda fqn: "ent-123")

    def _put_odcs(entity_id, odcs_yaml):
        if contract_fails:
            raise RuntimeError("404 Not Found")

    monkeypatch.setattr(registrar, "_put_odcs_yaml", _put_odcs)
    return registrar


def test_contract_registration_failure_is_marked_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registrar = _registrar(monkeypatch, contract_fails=True)
    payload = CatalogPublicationPayload.from_contract(CONTRACT, {})

    result = registrar.register_payload(payload)

    # The table publish really did succeed, so this is not a failure...
    assert result.succeeded is True
    # ...but it is not a complete success either, and the result says so.
    assert result.metadata.get("partial") is True
    assert "customers_raw" in result.metadata.get("odcs_contract_degraded", {})


def test_contract_registration_failure_is_visible_at_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The explanation only appeared at DEBUG; at INFO the operator saw three
    httpx lines and 'ok=True'."""
    registrar = _registrar(monkeypatch, contract_fails=True)
    payload = CatalogPublicationPayload.from_contract(CONTRACT, {})

    with caplog.at_level(logging.INFO, logger="fluid.acquire.catalog.openmetadata"):
        registrar.register_payload(payload)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the degrade produced no operator-visible record at INFO"
    assert "NOT registered" in warnings[0].getMessage()


def test_a_clean_publish_is_not_marked_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    registrar = _registrar(monkeypatch, contract_fails=False)
    payload = CatalogPublicationPayload.from_contract(CONTRACT, {})

    result = registrar.register_payload(payload)

    assert result.succeeded is True
    assert "partial" not in result.metadata
    assert "odcs_contract_degraded" not in result.metadata
