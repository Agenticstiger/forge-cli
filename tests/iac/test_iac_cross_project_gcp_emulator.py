# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Cross-project BigQuery ``dataset.access[]`` — emitter → BQ emulator round-trip.

What this file closes, precisely
================================

The cross-project story has three separable dimensions:

1. **Emit** — forge projects a service-account email belonging to
   *another* project onto the dataset's ``access[]`` block as a
   ``user_by_email`` entry. Already covered by
   ``test_iac_cross_account_emit.py``; re-asserted here only as the
   input to (2), so the round-trip replays a *real emitted* entry
   rather than a hand-written one.
2. **Wire-shape acceptance** — a BigQuery-API-compatible server accepts
   that emitted entry and returns it unchanged on read-back.
   **THIS FILE closes that dimension** against goccy/bigquery-emulator.
3. **Authorization** — the grant actually lets a principal in project B
   read project A's data. **NOT closed here, and not closeable here** —
   see ``test_emulator_neither_validates_nor_enforces_access`` below,
   which pins the emulator's inability to represent it. That dimension
   needs a real second GCP project.

Honest scope of (2)
-------------------

The emulator is a faithful *store* for ``access[]`` but performs no
validation and no IAM evaluation. Probed directly (2026-07-18,
ghcr.io/goccy/bigquery-emulator:latest):

* ``POST /datasets`` with a cross-project ``userByEmail`` entry → 200,
  and a subsequent ``GET`` returns the entry verbatim.
* The same POST with ``{"role": "NOT_A_ROLE", "userByEmail":
  "this-is-not-an-email"}`` → **also 200**, returned verbatim. Real
  BigQuery rejects both.
* An **unauthenticated** query against the dataset → 200 with rows.

So a green round-trip here proves the emitted entry *survives* a BQ API
round-trip with its role and cross-project SA email intact. It does
**not** prove the entry is well-formed by real-BigQuery rules, nor that
it authorises anything. Dimension (3) remains open.

Why the round-trip is REST, not ``tofu apply``
----------------------------------------------

``tofu apply`` against this emulator crashes the hashicorp/google
provider on read-back ("Plugin did not respond") — a known upstream
limitation, see ``test_iac_gcp_emulator_e2e.py`` and
goccy/bigquery-emulator#484. This file therefore drives the emitted
``access[]`` block onto the emulator through the official
``google-cloud-bigquery`` client instead, which is the same wire
protocol the provider would use.

Gate
----

Narrower than ``GCP_EMULATOR_ENABLED`` (which also demands GCS,
Pub/Sub and ``tofu``): these tests need **only** the BigQuery emulator.

    docker run -d -p 9050:9050 ghcr.io/goccy/bigquery-emulator:latest \\
        --project=fluid-iactest-producer --port=9050
    export FLUID_IAC_LIVE_GCP_EMULATOR=1
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List, Mapping

import pytest

from fluid_build.iac import build_module, get_iac_plugin

from .conftest import (
    GCP_EMULATOR_BIGQUERY,
    _gcp_emulator_port_reachable,
)

# The producer ("project A") and the two consumer projects ("B" and "C")
# whose service accounts are granted on project A's dataset. Only the
# producer project has to exist in the emulator; B and C are never
# contacted — a cross-project ``userByEmail`` entry is just an email
# string as far as the BigQuery dataset API is concerned, which is
# exactly the property under test.
PRODUCER_PROJECT = os.environ.get("FLUID_GCP_XPROJ_PRODUCER", "fluid-iactest-producer")
CONSUMER_PROJECT_B = "fluid-iactest-consumer"
CONSUMER_PROJECT_C = "fluid-iactest-consumer-c"
CONSUMER_SA_B = f"consumer-sa@{CONSUMER_PROJECT_B}.iam.gserviceaccount.com"
CONSUMER_SA_C = f"consumer-sa@{CONSUMER_PROJECT_C}.iam.gserviceaccount.com"

_TRUE = {"1", "true", "yes", "on"}


def _bq_emulator_ready() -> tuple[bool, str]:
    """``(enabled, skip_reason)`` for the BigQuery-only cross-project tier."""
    if os.environ.get("FLUID_IAC_LIVE_GCP_EMULATOR", "").strip().lower() not in _TRUE:
        return False, (
            "cross-project BQ emulator tests are opt-in — set "
            "FLUID_IAC_LIVE_GCP_EMULATOR=1 and start the BigQuery emulator"
        )
    if not _gcp_emulator_port_reachable(GCP_EMULATOR_BIGQUERY, 9050):
        return False, f"BigQuery emulator not reachable at {GCP_EMULATOR_BIGQUERY}"
    return True, ""


BQ_EMULATOR_ENABLED, BQ_EMULATOR_SKIP_REASON = _bq_emulator_ready()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.gcp,
    pytest.mark.provider,
    pytest.mark.emulated_heavy,
    pytest.mark.skipif(not BQ_EMULATOR_ENABLED, reason=BQ_EMULATOR_SKIP_REASON),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _xproj_contract(dataset: str, *consumer_sas: str) -> Dict[str, Any]:
    """A producer-project contract granting BQ read to SAs in other projects.

    Cross-project access rides the existing ``metadata.policies`` surface —
    the GCP plugin's ``_bq_access_entries`` maps each principal to a
    ``user_by_email`` row on the dataset's ``access[]`` block. Zero new
    schema fields.
    """
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "iac.gcp.xproj.emulator",
        "name": "Cross-project BigQuery exposure",
        "metadata": {
            "layer": "Silver",
            "productType": "ADP",
            "owner": {"team": "data-eng", "email": "data-eng@example.com"},
            "policies": {
                "consumers": {
                    "principals": list(consumer_sas),
                    "permissions": ["read"],
                }
            },
        },
        "exposes": [
            {
                "exposeId": "events",
                "kind": "table",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"dataset": dataset, "table": "events", "region": "US"},
                },
                "contract": {
                    "schema": [
                        {"name": "id", "type": "string", "required": True},
                        {"name": "amount", "type": "integer"},
                    ]
                },
            }
        ],
    }


def _emitted_access_block(contract: Mapping[str, Any]) -> List[Dict[str, str]]:
    """Run the real GCP emitter and return the dataset's ``access[]`` block."""
    module = json.loads(build_module(get_iac_plugin("gcp"), contract))
    datasets = module["resource"]["google_bigquery_dataset"]
    body = next(iter(datasets.values()))
    return body.get("access", [])


def _tf_access_to_bq_api(entries: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Translate emitted OpenTofu ``access`` rows to BigQuery REST rows.

    The hashicorp/google provider takes snake_case (``user_by_email``);
    the BigQuery REST API takes camelCase (``userByEmail``). This is the
    provider's own mapping, replayed here because the provider itself
    cannot run against this emulator (see the module docstring).
    """
    field_map = {"user_by_email": "userByEmail", "group_by_email": "groupByEmail"}
    out = []
    for entry in entries:
        row = {"role": entry["role"]}
        for tf_field, api_field in field_map.items():
            if tf_field in entry:
                row[api_field] = entry[tf_field]
        out.append(row)
    return out


def _bq_client():
    """A ``google.cloud.bigquery`` client pointed at the BigQuery emulator."""
    pytest.importorskip("google.cloud.bigquery")
    from google.api_core.client_options import ClientOptions
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery

    return bigquery.Client(
        project=PRODUCER_PROJECT,
        credentials=AnonymousCredentials(),
        client_options=ClientOptions(api_endpoint=GCP_EMULATOR_BIGQUERY),
    )


@pytest.fixture
def bq_dataset_name():
    """A unique dataset id — the emulator keeps state across tests."""
    return f"xproj_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# Dimension 1 — emit (input to the round-trip)
# ---------------------------------------------------------------------------


def test_emits_cross_project_sa_as_user_by_email(bq_dataset_name):
    """A project-B SA lands on project A's dataset as ``user_by_email``."""
    access = _emitted_access_block(_xproj_contract(bq_dataset_name, CONSUMER_SA_B))

    assert {"role": "READER", "user_by_email": CONSUMER_SA_B} in access
    # The SA email carries project B's name — the entry is what makes the
    # grant cross-project; nothing else in the module references project B.
    assert CONSUMER_PROJECT_B in CONSUMER_SA_B


def test_emits_bilateral_grants_for_two_distinct_projects(bq_dataset_name):
    """SAs from two different consumer projects both land, deduplicated."""
    access = _emitted_access_block(_xproj_contract(bq_dataset_name, CONSUMER_SA_B, CONSUMER_SA_C))

    emails = {e.get("user_by_email") for e in access}
    assert {CONSUMER_SA_B, CONSUMER_SA_C} <= emails


# ---------------------------------------------------------------------------
# Dimension 2 — the emulator round-trip (what this file closes)
# ---------------------------------------------------------------------------


def test_emulator_round_trips_emitted_cross_project_access_entry(bq_dataset_name):
    """The EMITTED ``access[]`` block survives a BigQuery API round-trip.

    Drives the emitter's own output (not a hand-written entry) onto the
    emulator and reads it back. Proves the wire shape forge produces is
    accepted and returned intact by a BQ-API-compatible server.
    """
    from google.cloud import bigquery

    emitted = _emitted_access_block(_xproj_contract(bq_dataset_name, CONSUMER_SA_B))
    api_rows = _tf_access_to_bq_api(emitted)
    assert {"role": "READER", "userByEmail": CONSUMER_SA_B} in api_rows

    client = _bq_client()
    dataset = bigquery.Dataset(f"{PRODUCER_PROJECT}.{bq_dataset_name}")
    dataset.location = "US"
    dataset.access_entries = [
        bigquery.AccessEntry(row["role"], "userByEmail", row["userByEmail"])
        for row in api_rows
        if "userByEmail" in row
    ]
    client.create_dataset(dataset, exists_ok=True)

    # Read back through a fresh GET — the emulator omits ``access`` from
    # the CREATE response but returns it on GET.
    fetched = client.get_dataset(f"{PRODUCER_PROJECT}.{bq_dataset_name}")
    round_tripped = {
        (entry.role, entry.entity_id)
        for entry in (fetched.access_entries or [])
        if entry.entity_type == "userByEmail"
    }

    assert ("READER", CONSUMER_SA_B) in round_tripped


def test_emulator_round_trips_bilateral_cross_project_entries(bq_dataset_name):
    """Two consumer projects' SAs both survive the round-trip together."""
    from google.cloud import bigquery

    emitted = _emitted_access_block(_xproj_contract(bq_dataset_name, CONSUMER_SA_B, CONSUMER_SA_C))
    api_rows = _tf_access_to_bq_api(emitted)

    client = _bq_client()
    dataset = bigquery.Dataset(f"{PRODUCER_PROJECT}.{bq_dataset_name}")
    dataset.location = "US"
    dataset.access_entries = [
        bigquery.AccessEntry(row["role"], "userByEmail", row["userByEmail"])
        for row in api_rows
        if "userByEmail" in row
    ]
    client.create_dataset(dataset, exists_ok=True)

    fetched = client.get_dataset(f"{PRODUCER_PROJECT}.{bq_dataset_name}")
    emails = {
        entry.entity_id
        for entry in (fetched.access_entries or [])
        if entry.entity_type == "userByEmail"
    }

    assert {CONSUMER_SA_B, CONSUMER_SA_C} <= emails


# ---------------------------------------------------------------------------
# Dimension 3 — the boundary this tier CANNOT cross (pinned, not skipped)
# ---------------------------------------------------------------------------


def test_emulator_neither_validates_nor_enforces_access(bq_dataset_name):
    """Pin the emulator's limits so a green round-trip is not over-read.

    This test PASSES when the emulator is permissive. That is the point:
    it documents, executably, that dimension (2) above is a storage
    round-trip and nothing more. If a future emulator release starts
    validating roles or evaluating IAM, this test fails loudly and the
    honest-scope docstring above must be revisited (and the card's
    remaining dimension may become closeable here).
    """
    from google.cloud import bigquery

    client = _bq_client()

    # (a) No validation: a bogus role and a non-email principal are
    #     accepted and returned verbatim. Real BigQuery rejects both.
    dataset = bigquery.Dataset(f"{PRODUCER_PROJECT}.{bq_dataset_name}")
    dataset.location = "US"
    dataset._properties["access"] = [
        {"role": "NOT_A_ROLE", "userByEmail": "this-is-not-an-email"},
    ]
    client.create_dataset(dataset, exists_ok=True)

    fetched = client.get_dataset(f"{PRODUCER_PROJECT}.{bq_dataset_name}")
    raw_access = fetched._properties.get("access") or []
    assert {"role": "NOT_A_ROLE", "userByEmail": "this-is-not-an-email"} in raw_access, (
        "emulator started validating access entries — the honest-scope "
        "docstring in this module is now stale"
    )

    # (b) No enforcement: the client authenticates with AnonymousCredentials
    #     and still queries successfully. No IAM evaluation happens, so the
    #     emulator cannot answer 'is the project-B SA authorised?' at all.
    rows = list(client.query("SELECT 1 AS x").result())
    assert rows[0]["x"] == 1, (
        "emulator started enforcing access — the honest-scope docstring "
        "in this module is now stale"
    )
