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

"""Engine guard: roadmap catalogs are skipped, never served as demo data.

The bundled per-catalog connectors (Collibra, Alation, Azure Purview, Apache
Atlas, Confluent Schema Registry, the AWS/GCP/custom-REST demos, and DataHub)
ship illustrative *mock* metadata rather than performing a real catalog query.
Real discovery for every one of them flows through the generic ``mcp``
connector pointed at the catalog's MCP server, so :class:`MarketDiscoveryEngine`
must skip the bundled connectors entirely — otherwise ``fluid market`` would
surface fabricated data products as if they were live catalog results. These
tests pin that behaviour for all of them and assert the skip is announced
honestly in the logs.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from fluid_build.cli.market import MarketDiscoveryEngine

LOG = logging.getLogger("test.market.roadmap")

# The demo-only catalogs the engine must skip. Hardcoded here (rather than
# derived from the engine's private set) so this list independently *pins* the
# expectation: the equality test below fails loudly if the engine's
# ``_ROADMAP_CATALOGS`` ever drifts from this set. Real discovery for these
# catalogs flows through the generic ``mcp`` connector instead.
_ROADMAP_CATALOG_NAMES = [
    # Proprietary demo connectors.
    "alation",
    "apache_atlas",
    "azure_purview",
    "collibra",
    "confluent_schema_registry",
    # Cloud / generic demo connectors (real discovery is via the mcp connector).
    "aws_glue_data_catalog",
    "google_cloud_data_catalog",
    "custom_rest_api",
    # DataHub's bundled connector returned one demo product; real discovery is
    # the mcp connector pointed at mcp-server-datahub.
    "datahub",
]

# Per-catalog configs that WOULD let each connector's ``_connect_impl`` return
# True (and thus emit its mock products) if the engine did not skip it first.
# Pairing the skip assertion with valid creds is the whole point: it proves the
# guard fires *before* the connector can fabricate results.
_CREDS = {
    "azure_purview": {"account_name": "acct"},
    "apache_atlas": {"base_url": "http://x", "username": "u", "password": "p"},
    "confluent_schema_registry": {"url": "http://x"},
    "collibra": {"base_url": "http://x", "username": "u", "password": "p"},
    "alation": {"base_url": "http://x", "api_token": "t"},
    "aws_glue_data_catalog": {"region": "us-east-1"},
    "google_cloud_data_catalog": {"project_id": "test"},
    "custom_rest_api": {"base_url": "http://api.example.com"},
    "datahub": {"server_url": "http://x"},
}

_BASE = {"defaults": {"timeout_seconds": 30}, "cache": {"enabled": False}}


def test_roadmap_set_matches_pinned_demo_connectors():
    # Lazy import: reaching into ``_market_discovery_engine`` at module scope
    # before ``market`` is imported trips a circular import. By call time the
    # host module (imported at top) has fully initialised the engine module.
    from fluid_build.cli._market_discovery_engine import _ROADMAP_CATALOGS

    assert _ROADMAP_CATALOGS == frozenset(_ROADMAP_CATALOG_NAMES)
    # Every roadmap catalog must have a creds fixture, or the skip-with-creds
    # guarantee below is vacuous for the missing one.
    assert set(_CREDS) == set(_ROADMAP_CATALOG_NAMES)


@pytest.mark.parametrize("catalog_type", sorted(_ROADMAP_CATALOG_NAMES))
def test_roadmap_catalog_is_skipped_not_connected(catalog_type, caplog):
    config = {**_BASE, "catalogs": [catalog_type], catalog_type: _CREDS[catalog_type]}
    engine = MarketDiscoveryEngine(config, LOG)
    with caplog.at_level(logging.INFO, logger=LOG.name):
        asyncio.run(engine.initialize_connectors([catalog_type]))
    # Skipped before instantiation → never registered → no mock-data path.
    assert catalog_type not in engine.connectors
    assert engine.connectors == {}
    # And the skip is announced honestly, not silently.
    assert any(
        "roadmap" in r.getMessage().lower() and catalog_type in r.getMessage()
        for r in caplog.records
    )


def test_all_roadmap_catalogs_skipped_together_leaves_engine_empty():
    config = {**_BASE, "catalogs": list(_ROADMAP_CATALOG_NAMES), **_CREDS}
    engine = MarketDiscoveryEngine(config, LOG)
    asyncio.run(engine.initialize_connectors(list(_ROADMAP_CATALOG_NAMES)))
    assert engine.connectors == {}
    # No connectors → no health checker spun up either.
    assert engine.health_checker is None
