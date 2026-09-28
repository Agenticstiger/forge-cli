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

"""GCP sovereignty fails closed on a location the table cannot place, and a
Pub/Sub topic keeps its region.

Measured on the branch before this change, with ``sovereignty: {jurisdiction:
EU, enforcementMode: strict}`` and no ``allowedRegions``: a gcp binding in
``me-central2``, ``northamerica-south1`` or the ``asia`` multi-region gave
``fluid validate`` rc 0 (a warning, "jurisdiction: Unknown") and ``fluid
generate iac`` rc 0 with that location in the module. The vendored region
table (dgl/cloud-regions) lacks nine of Google's regions, among them three EU
ones (europe-north2, europe-west10, europe-west12), and no multi- or
dual-region but US and EU.

A gcp ``pubsub_topic`` binding's ``location.region`` was dropped: the emitted
``google_pubsub_topic`` had no ``message_storage_policy``, so messages could
be stored outside an ``allowedRegions: [europe-west1]`` policy that validate
and generate had both passed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

from fluid_build._errors import SovereigntyViolationError
from fluid_build.iac import get_iac_plugin
from fluid_build.policy.sovereignty import SovereigntyValidator, region_jurisdiction_map
from fluid_build.providers.gcp.util.sovereignty import resource_placements

pytestmark = pytest.mark.unit

#: BigQuery's region list (docs.cloud.google.com/bigquery/docs/locations,
#: read 2026-09-28), with the country each is in.
_GOOGLE_REGIONS = {
    "us-east5": "US",
    "us-south1": "US",
    "us-central1": "US",
    "us-west2": "US",
    "us-west4": "US",
    "northamerica-south1": "MX",
    "northamerica-northeast1": "CA",
    "us-east4": "US",
    "us-central2": "US",
    "us-west1": "US",
    "us-west3": "US",
    "southamerica-east1": "BR",
    "southamerica-west1": "CL",
    "us-east1": "US",
    "northamerica-northeast2": "CA",
    "asia-southeast3": "TH",
    "asia-south2": "IN",
    "asia-east2": "HK",
    "asia-southeast2": "ID",
    "australia-southeast2": "AU",
    "asia-south1": "IN",
    "asia-northeast2": "JP",
    "asia-northeast3": "KR",
    "asia-southeast1": "SG",
    "australia-southeast1": "AU",
    "asia-east1": "TW",
    "asia-northeast1": "JP",
    "europe-west1": "EU",
    "europe-west10": "EU",
    "europe-north1": "EU",
    "europe-west3": "EU",
    "europe-west2": "UK",
    "europe-southwest1": "EU",
    "europe-west8": "EU",
    "europe-west4": "EU",
    "europe-west9": "EU",
    "europe-north2": "EU",
    "europe-west12": "EU",
    "europe-central2": "EU",
    "europe-west6": "CH",
    "me-central2": "SA",
    "me-central1": "QA",
    "me-west1": "IL",
    "africa-south1": "ZA",
}


@pytest.mark.parametrize("region, jurisdiction", sorted(_GOOGLE_REGIONS.items()))
def test_every_bigquery_region_has_its_jurisdiction(region, jurisdiction):
    assert region_jurisdiction_map().get(region) == jurisdiction


@pytest.mark.parametrize(
    "location, jurisdiction",
    [("EUR4", "EU"), ("eur4", "EU"), ("NAM4", "US"), ("ASIA1", "JP"), ("EU", "EU"), ("US", "US")],
)
def test_a_multi_or_dual_region_within_one_jurisdiction_resolves(location, jurisdiction):
    assert region_jurisdiction_map().get(location) == jurisdiction


@pytest.mark.parametrize("location", ["asia", "ASIA", "EUR5", "EUR7", "eur8"])
def test_a_location_spanning_jurisdictions_stays_unknown(location):
    """ASIA spans several countries; EUR5/EUR7/EUR8 each pair an EU region
    with London or Zürich."""
    assert region_jurisdiction_map().get(location) is None


_EU_STRICT = {"jurisdiction": "EU", "enforcementMode": "strict"}


def _contract(
    location: Dict[str, Any],
    *,
    fmt: str = "bigquery_table",
    sovereignty: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "bronze.unknown_probe",
        "name": "Unknown Probe",
        "domain": "Customer",
        "metadata": {"layer": "Bronze", "owner": {"team": "data-platform"}},
        "exposes": [
            {
                "exposeId": "t",
                "kind": "table",
                "binding": {"platform": "gcp", "format": fmt, "location": location},
                "contract": {"schema": [{"name": "a", "type": "STRING"}]},
            }
        ],
    }
    if sovereignty is not None:
        doc["sovereignty"] = sovereignty
    return doc


def _bq(region: str) -> Dict[str, Any]:
    return {"project": "p", "dataset": "d", "table": "t", "region": region}


def _emit(contract: Dict[str, Any]) -> Dict[str, Any]:
    return get_iac_plugin("gcp").emit(contract, [])


@pytest.mark.parametrize("region", ["me-central2", "northamerica-south1", "asia", "EUR5"])
def test_a_strict_eu_policy_refuses_a_location_it_cannot_place(region):
    ok, violations = SovereigntyValidator().validate(_contract(_bq(region), sovereignty=_EU_STRICT))
    assert ok is False
    assert any(
        v.severity == "error" and "does not match required jurisdiction" in v.message
        for v in violations
    )
    with pytest.raises(SovereigntyViolationError):
        _emit(_contract(_bq(region), sovereignty=_EU_STRICT))


@pytest.mark.parametrize("region", ["europe-west10", "europe-west12", "europe-north2", "EUR4"])
def test_an_eu_location_the_vendored_table_lacked_now_passes_cleanly(region):
    ok, violations = SovereigntyValidator().validate(_contract(_bq(region), sovereignty=_EU_STRICT))
    assert ok is True
    assert not [v for v in violations if "jurisdiction" in v.message]
    (dataset,) = _emit(_contract(_bq(region), sovereignty=_EU_STRICT))[
        "google_bigquery_dataset"
    ].values()
    assert dataset["location"] == region


def test_advisory_still_only_warns_about_a_location_it_cannot_place():
    policy = dict(_EU_STRICT, enforcementMode="advisory")
    ok, _ = SovereigntyValidator().validate(_contract(_bq("asia"), sovereignty=policy))
    assert ok is True
    _emit(_contract(_bq("asia"), sovereignty=policy))


# ── Pub/Sub: the binding's region is where messages may be stored ───────


def _topic(region: Optional[str] = "europe-west1") -> Dict[str, Any]:
    loc: Dict[str, Any] = {"project": "p", "topic": "customer-events"}
    if region:
        loc["region"] = region
    return loc


_PINNED = {"jurisdiction": "EU", "allowedRegions": ["europe-west1"], "enforcementMode": "strict"}


def test_a_pubsub_binding_s_region_becomes_its_message_storage_policy():
    resources = _emit(_contract(_topic(), fmt="pubsub_topic", sovereignty=_PINNED))
    (topic,) = resources["google_pubsub_topic"].values()
    assert topic["message_storage_policy"] == {"allowed_persistence_regions": ["europe-west1"]}


def test_without_a_policy_the_region_is_applied_too():
    """Never dropped: the field is the platform's to apply, policy or not."""
    (topic,) = _emit(_contract(_topic(), fmt="pubsub_topic"))["google_pubsub_topic"].values()
    assert topic["message_storage_policy"]["allowed_persistence_regions"] == ["europe-west1"]


def test_a_topic_with_no_region_has_no_storage_policy_and_no_policy_passes():
    (topic,) = _emit(_contract(_topic(None), fmt="pubsub_topic"))["google_pubsub_topic"].values()
    assert "message_storage_policy" not in topic


def test_a_pubsub_region_outside_the_policy_is_refused():
    with pytest.raises(SovereigntyViolationError) as exc:
        _emit(_contract(_topic("us-central1"), fmt="pubsub_topic", sovereignty=_PINNED))
    assert "us-central1" in exc.value.what


def test_a_pubsub_binding_with_no_region_is_refused_under_strict():
    with pytest.raises(SovereigntyViolationError) as exc:
        _emit(_contract(_topic(None), fmt="pubsub_topic", sovereignty=_PINNED))
    assert "declares no region" in exc.value.what


def test_the_hook_reads_the_persistence_regions_back():
    resources = {
        "google_pubsub_topic": {
            "a": {"name": "a", "message_storage_policy": {"allowed_persistence_regions": ["x1"]}},
            "b": {
                "name": "b",
                "message_storage_policy": [{"allowed_persistence_regions": ["y1", "${var.r}"]}],
            },
        }
    }
    assert resource_placements(resources) == [
        ("google_pubsub_topic.a", "x1"),
        ("google_pubsub_topic.b", "y1"),
    ]


# ── Through the real CLI ───────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    for var in ("FLUID_PROVIDER", "FLUID_REGION", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _cli(*argv: str) -> int:
    from fluid_build.cli import main

    return main(list(argv))


def _write(root: Path, contract: Dict[str, Any]) -> Path:
    path = root / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def test_validate_and_generate_refuse_me_central2_under_a_strict_eu_policy(workspace):
    path = _write(workspace, _contract(_bq("me-central2"), sovereignty=_EU_STRICT))
    assert _cli("validate", str(path)) == 1
    assert _cli("generate", "iac", str(path), "--out", str(workspace / "iac")) != 0
    assert not (workspace / "iac" / "main.tf.json").exists()


def test_generate_writes_the_topic_s_storage_policy(workspace):
    path = _write(workspace, _contract(_topic(), fmt="pubsub_topic", sovereignty=_PINNED))
    assert _cli("validate", str(path)) == 0
    assert _cli("generate", "iac", str(path), "--out", str(workspace / "iac")) == 0
    module = (workspace / "iac" / "main.tf.json").read_text(encoding="utf-8")
    assert '"allowed_persistence_regions"' in module
