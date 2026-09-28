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

"""``fluid verify`` on BigQuery: partition expiration, kmsKeyName and policy tags.

Before this, ``verify_bigquery_table`` checked schema and location only, so a
table with no retention, no key and no column restriction passed ``--strict``
whatever the contract declared. The dataset and table here are real
``google.cloud.bigquery`` ``Dataset`` / ``Table`` objects built from the REST
representation BigQuery returns (``from_api_repr``), so the attribute paths are the
library's; the Data Catalog calls go to a recorded fake session. No emulator:
goccy's does not implement policy tags or CMEK, and nothing here needs a network.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

import pytest

bigquery = pytest.importorskip("google.cloud.bigquery")

from fluid_build.cli import _verify_bigquery_governance as bqgov  # noqa: E402
from fluid_build.cli.verify import verify_bigquery_table  # noqa: E402

PROJECT = "northwind-demo"
KEY = (
    "projects/northwind-demo/locations/europe-west1/keyRings/"
    "fluid-gold_retention_candidates-demo_gold/cryptoKeys/bigquery"
)
TAG = "projects/northwind-demo/locations/europe-west1/taxonomies/111/policyTags/222"
PLATFORM = "group:data-platform@northwind.com"
PIPELINE = "serviceAccount:fluid-pipeline@northwind-demo.iam.gserviceaccount.com"
ANALYSTS = "group:analysts@northwind.com"


def _contract(**expose: Any) -> Dict[str, Any]:
    binding = {
        "platform": "gcp",
        "format": "bigquery_table",
        "location": {
            "project": PROJECT,
            "dataset": "demo_gold",
            "table": "retention_candidates",
            "region": "europe-west1",
        },
        "principals": {
            "group:data-platform@northwind.example": PLATFORM,
            "group:analysts@northwind.example": ANALYSTS,
            "serviceAccount:fluid-pipeline@northwind.example": PIPELINE,
        },
        **expose.pop("binding", {}),
    }
    exposure = {
        "exposeId": "candidates",
        "binding": binding,
        "contract": {
            "schema": [
                {"name": "customer_id", "type": "string", "required": True},
                {"name": "msisdn", "type": "string"},
            ]
        },
        **expose,
    }
    return {
        "id": "gold.retention_candidates",
        "accessPolicy": {
            "grants": [
                {"principal": "group:data-platform@northwind.example", "permissions": ["read"]},
                {"principal": "group:analysts@northwind.example", "permissions": ["read"]},
                {
                    "principal": "serviceAccount:fluid-pipeline@northwind.example",
                    "permissions": ["read", "write"],
                },
            ]
        },
        "exposes": [exposure],
    }


GOVERNED = dict(
    lifecycle={"retention": "P90D", "expire": True},
    binding={"encryption": {"kms": "product"}},
    policy={
        "authz": {
            "columnRestrictions": [
                {
                    "principal": "group:analysts@northwind.example",
                    "columns": ["msisdn"],
                    "access": "deny",
                }
            ]
        }
    },
)


def _table(
    *,
    partitioning: Optional[Dict[str, Any]] = None,
    kms: Optional[str] = KEY,
    tag: Optional[str] = TAG,
    expires: Optional[str] = None,
) -> Any:
    msisdn: Dict[str, Any] = {"name": "msisdn", "type": "STRING", "mode": "NULLABLE"}
    if tag:
        msisdn["policyTags"] = {"names": [tag]}
    resource: Dict[str, Any] = {
        "tableReference": {
            "projectId": PROJECT,
            "datasetId": "demo_gold",
            "tableId": "retention_candidates",
        },
        "schema": {
            "fields": [{"name": "customer_id", "type": "STRING", "mode": "REQUIRED"}, msisdn]
        },
        "numRows": "3",
    }
    if partitioning is not None:
        resource["timePartitioning"] = partitioning
    if kms:
        resource["encryptionConfiguration"] = {"kmsKeyName": kms}
    if expires:
        resource["expirationTime"] = expires
    return bigquery.Table.from_api_repr(resource)


def _dataset(kms: Optional[str] = KEY) -> Any:
    resource: Dict[str, Any] = {
        "datasetReference": {"projectId": PROJECT, "datasetId": "demo_gold"},
        "location": "europe-west1",
    }
    if kms:
        resource["defaultEncryptionConfiguration"] = {"kmsKeyName": kms}
    return bigquery.Dataset.from_api_repr(resource)


PARTITIONED = {"type": "DAY", "expirationMs": str(90 * 86_400_000)}


class _Response:
    def __init__(self, status: int, body: Dict[str, Any]) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Dict[str, Any]:
        return self._body


class _Catalog:
    """Records Data Catalog calls; answers the tag's IAM policy and its taxonomy."""

    def __init__(self, readers: List[str], *, enforced: bool = True, status: int = 200) -> None:
        self.readers = readers
        self.enforced = enforced
        self.status = status
        self.calls: List[str] = []

    def post(self, url: str, json: Any = None) -> _Response:
        self.calls.append(f"POST {url}")
        return _Response(
            self.status,
            {
                "bindings": [
                    {"role": "roles/datacatalog.categoryFineGrainedReader", "members": self.readers}
                ]
            },
        )

    def get(self, url: str) -> _Response:
        self.calls.append(f"GET {url}")
        types = ["FINE_GRAINED_ACCESS_CONTROL"] if self.enforced else []
        return _Response(self.status, {"activatedPolicyTypes": types})


def _dims(contract: Dict[str, Any], table: Any, dataset: Any, catalog: Any) -> Dict[str, Any]:
    return bqgov.governance_dimensions(
        contract["exposes"][0],
        contract=contract,
        bq_dataset=dataset,
        bq_table=table,
        project=PROJECT,
        session_factory=lambda: catalog,
    )


def test_a_governed_table_passes_all_three():
    catalog = _Catalog([PLATFORM, PIPELINE])
    dims = _dims(
        _contract(**copy.deepcopy(GOVERNED)), _table(partitioning=PARTITIONED), _dataset(), catalog
    )
    assert {name: d["status"] for name, d in dims.items()} == {
        "retention": "pass",
        "encryption": "pass",
        "columnRestrictions": "pass",
    }
    assert catalog.calls == [
        f"POST https://datacatalog.googleapis.com/v1/{TAG}:getIamPolicy",
        "GET https://datacatalog.googleapis.com/v1/projects/northwind-demo/locations/europe-west1/taxonomies/111",
    ]


def test_nothing_declared_nothing_checked():
    assert _dims(_contract(), _table(), _dataset(), _Catalog([])) == {}


@pytest.mark.parametrize(
    "partitioning, expires, fragment",
    [
        (None, None, "not partitioned"),
        ({"type": "DAY", "expirationMs": str(30 * 86_400_000)}, None, "after 30 days"),
        ({"type": "DAY"}, None, "expire never"),
        ({"type": "MONTH", "expirationMs": str(90 * 86_400_000)}, None, "not by DAY"),
        (PARTITIONED, "1790000000000", "deletes the whole product"),
    ],
)
def test_retention_fails_when_partitions_do_not_expire_as_declared(partitioning, expires, fragment):
    contract = _contract(lifecycle={"retention": "P90D", "expire": True})
    dims = _dims(
        contract, _table(partitioning=partitioning, expires=expires), _dataset(), _Catalog([])
    )
    assert dims["retention"]["status"] == "fail"
    assert fragment in dims["retention"]["message"]


def test_retention_checks_the_declared_partition_column():
    contract = _contract(
        lifecycle={"retention": "P90D", "expire": True},
        binding={
            "location": {**_contract()["exposes"][0]["binding"]["location"], "partitionBy": ["ts"]}
        },
    )
    contract["exposes"][0]["contract"]["schema"].append({"name": "ts", "type": "timestamp"})
    dims = _dims(contract, _table(partitioning=PARTITIONED), _dataset(), _Catalog([]))
    assert "partitioned on ingestion time, not on ts" in dims["retention"]["message"]


@pytest.mark.parametrize(
    "table_kms, dataset_kms, fragment",
    [
        (None, KEY, "the table is encrypted with a Google-managed key"),
        (KEY, None, "the dataset's default key is a Google-managed key"),
        (KEY.replace("bigquery", "other"), KEY, "the table is encrypted with"),
    ],
)
def test_encryption_fails_on_the_wrong_key(table_kms, dataset_kms, fragment):
    contract = _contract(binding={"encryption": {"kms": "product"}})
    dims = _dims(contract, _table(kms=table_kms), _dataset(dataset_kms), _Catalog([]))
    assert dims["encryption"]["status"] == "fail"
    assert fragment in dims["encryption"]["message"]


def test_a_key_version_in_kms_key_name_still_matches():
    contract = _contract(binding={"encryption": {"kms": "product"}})
    dims = _dims(contract, _table(kms=f"{KEY}/cryptoKeyVersions/3"), _dataset(), _Catalog([]))
    assert dims["encryption"]["status"] == "pass"


@pytest.mark.parametrize(
    "readers, tag, enforced, fragment",
    [
        ([PLATFORM, PIPELINE, ANALYSTS], TAG, True, f"{ANALYSTS} can read msisdn"),
        ([PLATFORM], TAG, True, f"{PIPELINE} cannot read msisdn"),
        ([PLATFORM, PIPELINE], None, True, "carries no policy tag"),
        ([PLATFORM, PIPELINE], TAG, False, "does not enforce fine-grained access control"),
    ],
)
def test_column_restrictions_fail_when_the_tag_does_not_hold(readers, tag, enforced, fragment):
    contract = _contract(policy=copy.deepcopy(GOVERNED["policy"]))
    dims = _dims(contract, _table(tag=tag), _dataset(), _Catalog(readers, enforced=enforced))
    assert dims["columnRestrictions"]["status"] == "fail"
    assert fragment in dims["columnRestrictions"]["message"]


def test_a_catalog_that_cannot_be_read_is_an_error_not_a_pass():
    contract = _contract(policy=copy.deepcopy(GOVERNED["policy"]))
    dims = _dims(contract, _table(), _dataset(), _Catalog([PLATFORM, PIPELINE], status=403))
    assert dims["columnRestrictions"]["status"] == "error"
    assert "HTTP 403" in dims["columnRestrictions"]["message"]


# ── through verify_bigquery_table ────────────────────────────────────────


class _Client:
    def __init__(self, table: Any, dataset: Any) -> None:
        self._table, self._dataset = table, dataset

    def get_table(self, _table_id: str) -> Any:
        return self._table

    def get_dataset(self, _dataset_id: str) -> Any:
        return self._dataset


def _verify(monkeypatch, contract, table, dataset, catalog) -> Dict[str, Any]:
    monkeypatch.setattr(bigquery, "Client", lambda project=None: _Client(table, dataset))
    exposure = contract["exposes"][0]
    return verify_bigquery_table(
        PROJECT,
        "demo_gold",
        "retention_candidates",
        exposure["contract"]["schema"],
        "europe-west1",
        expose=exposure,
        contract=contract,
        catalog_session_factory=lambda: catalog,
    )


def test_verify_bigquery_table_is_critical_on_a_governance_mismatch(monkeypatch):
    contract = _contract(**copy.deepcopy(GOVERNED))
    result = _verify(
        monkeypatch, contract, _table(partitioning=None), _dataset(), _Catalog([PLATFORM, PIPELINE])
    )
    assert result["status"] == "mismatch"
    assert result["severity"]["level"] == "CRITICAL"
    assert "not partitioned" in result["severity"]["reason"]
    assert result["dimensions"]["structure"]["status"] == "pass"
    assert result["dimensions"]["retention"]["status"] == "fail"


def test_verify_bigquery_table_matches_a_governed_table(monkeypatch):
    contract = _contract(**copy.deepcopy(GOVERNED))
    result = _verify(
        monkeypatch,
        contract,
        _table(partitioning=PARTITIONED),
        _dataset(),
        _Catalog([PLATFORM, PIPELINE]),
    )
    assert result["status"] == "match", result


def test_verify_bigquery_table_errors_when_a_policy_cannot_be_checked(monkeypatch):
    contract = _contract(**copy.deepcopy(GOVERNED))
    result = _verify(
        monkeypatch,
        contract,
        _table(partitioning=PARTITIONED),
        _dataset(),
        _Catalog([PLATFORM, PIPELINE], status=500),
    )
    assert result["status"] == "error"
    assert "HTTP 500" in result["error"]
