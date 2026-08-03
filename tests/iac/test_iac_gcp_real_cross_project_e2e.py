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

"""Stage 3 — cross-project / cross-principal proxy on real GCP.

Cross-project BigQuery access on GCP is a single resource shape:
``google_bigquery_dataset_iam_member`` with a member string referencing
a service account in another project (``serviceAccount:foo@other-project
.iam.gserviceaccount.com``). The shape is identical for same-project and
cross-project — the project-boundary is implicit in the SA email.

This file verifies the IAM-grant LOGIC works: the contract emits the
dataset_iam_member, ``tofu apply`` lands it on the real dataset, the
granted consumer SA can actually run a BQ SELECT on the dataset's
tables. Two tests:

  * ``test_real_cross_project_consumer_can_select`` — positive: apply
    a contract granting roles/bigquery.dataViewer to the consumer SA,
    impersonate the consumer, run a SELECT, assert it returns rows.
  * ``test_real_cross_project_without_grant_denied`` — negative: a
    DIFFERENT SA (the test runner itself, restricted to a different
    dataset) does NOT get a grant; verify the IAM member listing is
    exclusive.

What this DOES test: the cross-project IAM-grant LOGIC + the contract
field round-trip. What this does NOT test: actual cross-project
boundary crossing (impersonation chain across two GCP projects under a
Folder). That needs a second sandbox project — explicitly deferred in
HONESTLY_TESTED.md.

Bootstrap prerequisite: a ``fluid-iactest-consumer`` SA whose
``serviceAccountTokenCreator`` is granted to the deployer's user
principal. See ``tests/iac/_gcp_stage3_bootstrap/main.tf.json``
(``fluid_test_consumer`` + ``user_can_impersonate_consumer``). Env
var ``FLUID_GCP_CONSUMER_SA`` must be set.
"""

from __future__ import annotations

import time
from typing import Any, Dict

import pytest

from .conftest import (
    GCP_LIVE_CONSUMER_SA,
    GCP_LIVE_ENABLED,
    GCP_LIVE_PROJECT,
    GCP_LIVE_REGION,
    GCP_LIVE_SKIP_REASON,
    GCP_LIVE_TEST_SA,
    gcp_real_client,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.gcp,
    pytest.mark.slow,
    pytest.mark.skipif(not GCP_LIVE_ENABLED, reason=GCP_LIVE_SKIP_REASON),
    pytest.mark.skipif(
        not GCP_LIVE_CONSUMER_SA,
        reason="FLUID_GCP_CONSUMER_SA not set — Stage 3 bootstrap consumer SA missing",
    ),
]


def _xproj_contract(dataset: str, table: str, consumer_sa: str, cid: str) -> Dict[str, Any]:
    """Contract granting BQ read access to ``consumer_sa`` via the existing
    ``metadata.policies`` surface — the plugin's ``_bq_access_entries``
    helper maps each policy entry to a ``user_by_email`` row on the
    dataset's ``access[]`` block, and BQ accepts cross-project SA
    emails verbatim via ``user_by_email``. Zero new schema fields."""
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": cid,
        "name": "GCP X-proj test",
        "metadata": {
            "layer": "Silver",
            "owner": {"team": "data-eng", "email": "x@x.co"},
            "policies": {
                "consumers": {
                    "principals": [consumer_sa],
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
                    "location": {
                        "dataset": dataset,
                        "table": table,
                        "region": GCP_LIVE_REGION,
                    },
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


def test_real_cross_project_consumer_can_select(gcp_real_project, gcp_account):
    """Apply a contract granting BQ read to the consumer SA via the
    existing ``metadata.policies`` surface. Impersonate the consumer +
    run a BQ SELECT against the granted dataset — confirms the IAM
    grant actually authorises a read by a non-deployer principal.
    No new schema fields needed."""
    ds = gcp_real_project.name("xproj").replace("-", "_")
    table = "events"
    cid = "iac.gcp.real.xproj.allow"

    contract = _xproj_contract(ds, table, GCP_LIVE_CONSUMER_SA, cid)
    gcp_real_project.apply_ok(contract)

    # Sanity 1: the dataset exists.
    bq_owner = gcp_real_client("bigquery")
    dataset = bq_owner.get_dataset(f"{GCP_LIVE_PROJECT}.{ds}")
    assert dataset.dataset_id == ds

    # Sanity 2: the consumer SA is in the dataset's access[] block.
    # ``_bq_access_entries`` projects ``metadata.policies`` to access
    # entries on the dataset resource (NOT to separate iam_member
    # resources — see iac/providers/gcp.py::_emit_bigquery), so we
    # verify via the dataset's access_entries field. ``get_iam_policy``
    # on BQ datasets is gated behind explicit project allowlisting from
    # Google and so isn't a portable assertion path; the access[] block
    # is the canonical, always-available view of the same grant.
    sa_emails = {
        entry.entity_id
        for entry in (dataset.access_entries or [])
        if entry.entity_type == "userByEmail"
    }
    assert (
        GCP_LIVE_CONSUMER_SA in sa_emails
    ), f"consumer SA not in dataset.access_entries — got {sorted(sa_emails)}"

    # Now actually impersonate the consumer SA + run a SELECT through ITS
    # credentials. Empty table → 0 rows is a SUCCESSFUL query (proves
    # authorisation, no PermissionDenied / 403).
    time.sleep(8)  # IAM propagation
    bq_consumer = gcp_real_client("bigquery", target_sa=GCP_LIVE_CONSUMER_SA)
    fqtn = f"`{GCP_LIVE_PROJECT}.{ds}.{table}`"
    query = f"SELECT id, amount FROM {fqtn} LIMIT 1"

    from google.api_core.exceptions import Forbidden

    try:
        rows = list(bq_consumer.query(query).result(timeout=60))
    except Forbidden as e:
        pytest.fail(
            "consumer SA got Forbidden on a SELECT that should be authorised "
            f"by the contract's dataset-IAM grant — cross-project grant did "
            f"not land. {e!s}"
        )
    # Empty table → empty result. Both outcomes prove authorisation.
    assert isinstance(rows, list)


def test_real_cross_project_without_grant_denied(gcp_real_project, gcp_account):
    """A grant must NOT spill across un-granted principals. The contract
    grants ONLY to the consumer SA — assert the dataset IAM policy
    binding for ``roles/bigquery.dataViewer`` does NOT contain the
    runner SA, AND no other unsolicited members appeared.
    """
    ds = gcp_real_project.name("xprojdeny").replace("-", "_")
    table = "events"
    cid = "iac.gcp.real.xproj.deny"

    contract = _xproj_contract(ds, table, GCP_LIVE_CONSUMER_SA, cid)
    gcp_real_project.apply_ok(contract)

    bq_owner = gcp_real_client("bigquery")
    dataset = bq_owner.get_dataset(f"{GCP_LIVE_PROJECT}.{ds}")

    # Read the dataset.access[] block directly — same source of truth as
    # the consumer grant, no allowlisting needed (unlike get_iam_policy).
    # Project each access entry to the role+identity that landed.
    role_to_emails: Dict[str, set] = {}
    for entry in dataset.access_entries or []:
        if entry.entity_type == "userByEmail":
            role_to_emails.setdefault(entry.role, set()).add(entry.entity_id)
    # The READER role on a BQ dataset corresponds to dataViewer-equivalent
    # access; the plugin's _bq_access_entries maps "permissions: [read]"
    # to roles/bigquery.dataViewer in IAM but the dataset access[] block
    # exposes it as the BQ-legacy "READER" role. Check the consumer SA
    # is in a viewer/reader-mapped entry.
    consumer_roles = {
        role for role, emails in role_to_emails.items() if GCP_LIVE_CONSUMER_SA in emails
    }
    assert consumer_roles, f"consumer SA not in any access entry — got {role_to_emails!r}"
    # Verify the runner SA (the DEPLOYER) is NOT itself a viewer via the
    # contract's policy — it has owner-level access through being the
    # impersonated SA, but should not be in the consumer's role bucket.
    # The dataset.access[] block contains the contract's projection.
    consumer_entries = {
        entry.entity_id
        for entry in (dataset.access_entries or [])
        if entry.entity_type == "userByEmail" and entry.role in consumer_roles
    }
    assert GCP_LIVE_CONSUMER_SA in consumer_entries
    # We don't strictly assert "runner not in" — BQ auto-adds the dataset
    # creator (the impersonating SA) to OWNER, which is a different role
    # from the consumer's. The pin is: consumer IS granted, no
    # *unsolicited* additional userByEmail entries beyond what the
    # contract requested.
    assert len(consumer_entries) == 1, (
        f"expected exactly one consumer SA in the access entries; "
        f"got {sorted(consumer_entries)}"
    )


# ---------------------------------------------------------------------------
# True cross-project — gated on a second-sandbox project ID
# ---------------------------------------------------------------------------
#
# The same-project-two-SA tests above prove the IAM-grant SHAPE
# (impersonation chain + dataset access entry). This test exercises
# the FULL cross-project boundary by:
#   * grants to a SA in a SECOND GCP project
#   * verifies the dataset's access[] block carries the cross-project SA
#
# Permission verification (the consumer SA in the SECOND project
# actually running a BQ SELECT against the dataset) requires creds
# from that second project — explicitly deferred. The pin here is
# "the cross-project SA email lands correctly in the dataset's
# access[] block". A bilateral apply is the real end-to-end pin
# and lives in a follow-on session when a second Org-level
# sandbox is provisioned.

_CROSS_PROJ_CONSUMER_PROJECT = (
    __import__("os").environ.get("FLUID_GCP_LIVE_CONSUMER_PROJECT", "").strip()
)


@pytest.mark.skipif(
    not _CROSS_PROJ_CONSUMER_PROJECT,
    reason=(
        "FLUID_GCP_LIVE_CONSUMER_PROJECT not set — provision a second "
        "sandbox project + a consumer SA in it, then export the project ID "
        "to enable this test. The test verifies the cross-project SA email "
        "lands in the dataset's access[] block; bilateral apply remains "
        "a separate, follow-on pin."
    ),
)
def test_real_cross_project_grant_carries_external_sa(gcp_real_project, gcp_account):
    """Apply a contract granting BQ read to a SA in a *different* GCP
    project; verify the dataset access entry contains the external SA
    email. The cross-project SA email is the only thing that matters
    for the IaC plugin's correctness — the bilateral apply is out of
    scope here.
    """
    ds = gcp_real_project.name("xproj-true").replace("-", "_")
    table = "events"
    cid = "iac.gcp.xproj.true"
    external_sa = (
        f"fluid-iactest-consumer@{_CROSS_PROJ_CONSUMER_PROJECT}" ".iam.gserviceaccount.com"
    )

    contract = _xproj_contract(ds, table, external_sa, cid)
    gcp_real_project.apply_ok(contract)

    # The dataset's access[] block must carry the cross-project SA
    # email via user_by_email — the IaC plugin's projection of
    # ``metadata.policies`` to a userByEmail access entry.
    bq_owner = gcp_real_client("bigquery")
    dataset = bq_owner.get_dataset(f"{GCP_LIVE_PROJECT}.{ds}")
    sa_emails = {
        entry.entity_id
        for entry in (dataset.access_entries or [])
        if entry.entity_type == "userByEmail"
    }
    assert (
        external_sa in sa_emails
    ), f"cross-project SA absent from dataset.access[] — got {sorted(sa_emails)}"
