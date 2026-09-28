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

"""Sovereignty fails closed on GCP.

Measured on 0.16.5 against the demo's bronze contract (``jurisdiction: EU``,
``allowedRegions: [eu-north-1, eu-west-1, europe-west1]``, strict):

* a gcp overlay with no region validated (rc 0), ``plan --check-sovereignty``
  printed PASS, and the emitted dataset landed in ``US``;
* ``us-central1`` was refused by ``fluid validate`` only: ``fluid generate
  iac`` emitted it, rc 0, because the GCP provider had no sovereignty hook;
* a region given as ``location.location`` (which the GCP emitter reads) was
  never checked at all.

Each is pinned here against the real CLI entry points, the GCP IaC plugin
and the GCP provider. No cloud is called: the plugin and the provider only
compute, and the BigQuery client is faked.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

from fluid_build._errors import SovereigntyViolationError
from fluid_build.iac import get_iac_plugin
from fluid_build.policy.sovereignty import SovereigntyValidator, binding_region

pytestmark = pytest.mark.unit

_LOG = logging.getLogger("test.gcp_sovereignty")

_SOVEREIGNTY = {
    "jurisdiction": "EU",
    "allowedRegions": ["eu-north-1", "eu-west-1", "europe-west1"],
    "deniedRegions": ["us-east-1", "us-west-2"],
    "dataResidency": True,
    "crossBorderTransfer": False,
    "enforcementMode": "strict",
}


def _contract(
    location: Dict[str, Any],
    *,
    mode: Optional[str] = "strict",
    platform: str = "gcp",
    sovereignty: bool = True,
) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "bronze.customer_subscriptions",
        "name": "Customer Subscriptions",
        "description": "Sovereignty fixture.",
        "domain": "Customer",
        "metadata": {
            "layer": "Bronze",
            "owner": {"team": "data-platform", "email": "dp@example.com"},
        },
        "exposes": [
            {
                "exposeId": "subscriptions",
                "kind": "table",
                "binding": {
                    "platform": platform,
                    "format": "bigquery_table" if platform == "gcp" else "parquet",
                    "location": location,
                },
                "contract": {"schema": [{"name": "subscription_id", "type": "STRING"}]},
            }
        ],
    }
    if sovereignty:
        doc["sovereignty"] = dict(_SOVEREIGNTY, enforcementMode=mode)
    return doc


_BQ = {"project": "northwind-demo", "dataset": "demo_bronze", "table": "customer_subscriptions"}


def _findings(contract: Dict[str, Any]):
    return SovereigntyValidator().validate(contract)


# ── The policy engine: fluid validate and plan --check-sovereignty ────────


def test_a_gcp_binding_with_no_region_is_refused_under_strict():
    ok, violations = _findings(_contract(dict(_BQ)))
    assert ok is False
    (v,) = violations
    assert v.severity == "error"
    assert "declares no region" in v.message
    assert v.expose_id == "subscriptions"


@pytest.mark.parametrize("mode, severity", [("advisory", "warning"), ("audit", "info")])
def test_the_mode_decides_like_it_does_for_every_other_check(mode, severity):
    ok, violations = _findings(_contract(dict(_BQ), mode=mode))
    assert ok is True
    assert [v.severity for v in violations] == [severity]


def test_a_local_binding_with_no_region_is_still_not_a_finding():
    """The demo's local base carries the same sovereignty block."""
    ok, violations = _findings(_contract({"path": "data/x.parquet"}, platform="local"))
    assert (ok, violations) == (True, [])


def test_an_aws_binding_with_no_region_is_refused_too():
    loc = {"bucket": "b", "database": "d", "table": "t", "path": "p/"}
    ok, _ = _findings(_contract(loc, platform="aws"))
    assert ok is False


def test_a_region_given_as_location_location_is_the_one_checked():
    """The GCP emitter falls back to ``location.location``; so must the check."""
    contract = _contract(dict(_BQ, location="us-central1"))
    assert binding_region(contract["exposes"][0]["binding"]) == "us-central1"
    ok, violations = _findings(contract)
    assert ok is False
    assert any("us-central1" in v.message for v in violations)


def test_the_bigquery_us_multi_region_is_us_jurisdiction():
    contract = _contract(dict(_BQ, region="US"))
    contract["sovereignty"].pop("allowedRegions")
    ok, violations = _findings(contract)
    assert ok is False
    assert any("jurisdiction: US" in v.message for v in violations)


def test_an_allowed_gcp_region_passes():
    assert _findings(_contract(dict(_BQ, region="europe-west1"))) == (True, [])


# ── The GCP OpenTofu plugin: fluid apply and fluid generate iac ────────────


def _emit(contract: Dict[str, Any]) -> Dict[str, Any]:
    return get_iac_plugin("gcp").emit(contract, [])


def test_the_emitter_refuses_to_guess_a_location_under_a_strict_policy():
    with pytest.raises(SovereigntyViolationError) as exc:
        _emit(_contract(dict(_BQ)))
    assert "subscriptions: Binding declares no region" in exc.value.what
    # The rendered panel is rich markup: no [..] that would be eaten.
    assert "[subscriptions]" not in exc.value.what


def test_the_emitter_refuses_an_out_of_jurisdiction_region():
    with pytest.raises(SovereigntyViolationError) as exc:
        _emit(_contract(dict(_BQ, region="us-central1")))
    assert "us-central1" in exc.value.what


def test_the_emitter_places_an_allowed_region():
    resources = _emit(_contract(dict(_BQ, region="europe-west1")))
    (dataset,) = resources["google_bigquery_dataset"].values()
    assert dataset["location"] == "europe-west1"


def test_without_a_policy_the_old_us_default_is_unchanged():
    resources = _emit(_contract(dict(_BQ), sovereignty=False))
    (dataset,) = resources["google_bigquery_dataset"].values()
    assert dataset["location"] == "US"


def test_under_advisory_the_us_default_is_emitted_and_said_out_loud(caplog):
    caplog.set_level(logging.WARNING)
    resources = _emit(_contract(dict(_BQ), mode="advisory"))
    (dataset,) = resources["google_bigquery_dataset"].values()
    assert dataset["location"] == "US"
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "declares no region" in said
    assert "Region 'US' not in allowed regions list" in said


# ── The GCP provider hook (native planner), the way AWS refuses ───────────


def test_the_provider_refuses_and_generate_iac_sees_a_sovereignty_veto(monkeypatch):
    from fluid_build.cli.generate_iac import _is_sovereignty_refusal
    from fluid_build.providers.base import ProviderError
    from fluid_build.providers.gcp.provider import GcpProvider

    provider = GcpProvider(project="northwind-demo", region="europe-west1")
    with pytest.raises(ProviderError) as exc:
        provider.plan(_contract(dict(_BQ, region="us-central1")))
    assert _is_sovereignty_refusal(exc.value)


def test_a_provider_default_region_is_checked_where_it_is_used():
    """``--region`` defaults to europe-west3 and the SDK to us-central1; a
    planned resource that inherits the provider's region is checked there."""
    from fluid_build.providers.base import ProviderError
    from fluid_build.providers.gcp.provider import GcpProvider

    provider = GcpProvider(project="northwind-demo", region="europe-west3")
    contract = _contract(dict(_BQ, region="europe-west1"))
    scheduled = [{"op": "scheduler.ensure_job", "id": "nightly", "location": provider.region}]
    with pytest.raises(ProviderError) as exc:
        provider._validate_sovereignty(contract, scheduled)
    assert "nightly: Region 'europe-west3' not in allowed regions list" in str(exc.value)
    # The same planned job in an allowed region passes.
    provider._validate_sovereignty(contract, [dict(scheduled[0], location="europe-west1")])


# ── Through the real CLI ───────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    for var in ("FLUID_PROVIDER", "FLUID_REGION", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(root: Path, contract: Dict[str, Any]) -> Path:
    path = root / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _cli(*argv: str) -> int:
    from fluid_build.cli import main

    return main(list(argv))


def test_validate_refuses_a_gcp_binding_with_no_region(workspace):
    assert _cli("validate", str(_write(workspace, _contract(dict(_BQ))))) == 1


def test_generate_iac_refuses_an_out_of_jurisdiction_region(workspace):
    contract = _write(workspace, _contract(dict(_BQ, region="us-central1")))
    assert _cli("generate", "iac", str(contract), "--out", str(workspace / "iac")) != 0
    assert not (workspace / "iac" / "main.tf.json").exists()


def test_generate_iac_emits_an_allowed_region(workspace):
    contract = _write(workspace, _contract(dict(_BQ, region="europe-west1")))
    assert _cli("generate", "iac", str(contract), "--out", str(workspace / "iac")) == 0
    assert '"europe-west1"' in (workspace / "iac" / "main.tf.json").read_text(encoding="utf-8")


def test_plan_check_sovereignty_blocks_a_gcp_binding_with_no_region(workspace, capsys):
    contract = _write(workspace, _contract(dict(_BQ)))
    rc = _cli("plan", str(contract), "--out", str(workspace / "plan.json"), "--check-sovereignty")
    assert rc == 1
    assert "PASS" not in capsys.readouterr().out


# ── The BigQuery load: no guessed location ────────────────────────────────


def test_a_load_with_no_region_runs_where_the_table_is(tmp_path, monkeypatch):
    from fluid_build.build_runners import _bigquery_load

    binding = copy.deepcopy(_contract(dict(_BQ))["exposes"][0]["binding"])
    target = _bigquery_load.bigquery_load_target(binding, {"exposeId": "subscriptions"})
    assert target is not None and target["location"] is None

    calls = []

    class _Job:
        output_rows = 1
        job_id = "job-1"

        def result(self):
            return None

    class _Table:
        schema: list = []
        location = "europe-west1"

    class _Client:
        project = "northwind-demo"

        def __init__(self, project=None):
            pass

        def get_table(self, table_id):
            return _Table()

        def load_table_from_file(self, fh, table_id, job_config=None, location=None):
            calls.append(location)
            return _Job()

    class _Module:
        Client = _Client

        class LoadJobConfig:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class SourceFormat:
            PARQUET = "PARQUET"

        class WriteDisposition:
            WRITE_APPEND = "WRITE_APPEND"
            WRITE_TRUNCATE = "WRITE_TRUNCATE"

    monkeypatch.setattr(_bigquery_load, "_bigquery_module", lambda: _Module)
    landed = tmp_path / "rows.parquet"
    landed.write_bytes(b"PAR1")
    mode = sorted(_bigquery_load._WRITE_DISPOSITION)[0]
    sink = sorted(_bigquery_load._SOURCE_FORMAT)[0]
    _bigquery_load.load_file(
        str(landed), target, mode=mode, sink_format=sink, expected_rows=1, logger=_LOG
    )
    assert calls == ["europe-west1"]


# ── A key ring and a taxonomy in a BigQuery multi-region ──────────────────
#
# Measured on the integration of this branch with the governance one: a
# contract with ``allowedRegions: [EU]`` and an ``EU`` dataset emits its CMEK
# key ring in the Cloud KMS location ``europe`` and its policy-tag taxonomy in
# the Data Catalog location ``eu``. ``fluid validate --strict`` and ``fluid plan
# --check-sovereignty`` passed, and ``fluid generate iac`` / ``fluid apply``
# refused both ("Region 'europe' not in allowed regions list", "(jurisdiction:
# Unknown)"). They are the dataset's own place.

_EU_ONLY = {"jurisdiction": "EU", "allowedRegions": ["EU"], "enforcementMode": "strict"}


def _multi_region_contract(region: str) -> Dict[str, Any]:
    contract = _contract(dict(_BQ, region=region))
    contract["sovereignty"] = dict(_EU_ONLY)
    return contract


def _governed(dataset_location: str, ring: str, taxonomy: str) -> Dict[str, Any]:
    return {
        "google_bigquery_dataset": {"d": {"location": dataset_location}},
        "google_kms_key_ring": {"k": {"name": "ring", "location": ring}},
        "google_data_catalog_taxonomy": {"t": {"display_name": "tax", "region": taxonomy}},
    }


def test_a_key_ring_and_a_taxonomy_are_placed_at_their_datasets_multi_region():
    from fluid_build.providers.gcp.util.sovereignty import resource_placements

    assert resource_placements(_governed("EU", "europe", "eu")) == [
        ("google_bigquery_dataset.d", "EU"),
        ("google_kms_key_ring.k", "EU"),
        ("google_data_catalog_taxonomy.t", "EU"),
    ]
    assert resource_placements(_governed("US", "us", "us"))[1:] == [
        ("google_kms_key_ring.k", "US"),
        ("google_data_catalog_taxonomy.t", "US"),
    ]
    # Spelled as the dataset spells it, so ``allowedRegions: [eu]`` agrees too.
    assert {p for _, p in resource_placements(_governed("eu", "europe", "eu"))} == {"eu"}
    # A regional key ring or taxonomy is where it says.
    assert resource_placements(_governed("europe-west1", "europe-west1", "europe-west1")) == [
        ("google_bigquery_dataset.d", "europe-west1"),
        ("google_kms_key_ring.k", "europe-west1"),
        ("google_data_catalog_taxonomy.t", "europe-west1"),
    ]


def test_an_eu_datasets_key_ring_and_taxonomy_pass_an_eu_only_strict_policy():
    from fluid_build.providers.gcp.util.sovereignty import (
        enforce_gcp_sovereignty,
        resource_placements,
    )

    contract = _multi_region_contract("EU")
    enforce_gcp_sovereignty(contract, resource_placements(_governed("EU", "europe", "eu")))
    # With only a jurisdiction, ``europe`` used to resolve to none (strict refuses).
    contract["sovereignty"] = {"jurisdiction": "EU", "enforcementMode": "strict"}
    enforce_gcp_sovereignty(contract, resource_placements(_governed("EU", "europe", "eu")))


def test_a_us_datasets_key_ring_and_taxonomy_are_still_refused_under_an_eu_policy():
    from fluid_build.providers.gcp.util.sovereignty import (
        enforce_gcp_sovereignty,
        resource_placements,
    )

    with pytest.raises(SovereigntyViolationError) as exc:
        enforce_gcp_sovereignty(
            _multi_region_contract("US"), resource_placements(_governed("US", "us", "us"))
        )
    assert "google_kms_key_ring.k: Region 'US' not in allowed regions list" in exc.value.what
    assert "(jurisdiction: US)" in exc.value.what


# ── fluid plan --check-sovereignty runs what fluid apply runs ─────────────


def _hook(contract: Dict[str, Any]):
    from fluid_build.providers.gcp.provider import GcpProvider

    return GcpProvider(project="northwind-demo", region="europe-west1").validate_sovereignty(
        contract
    )


def test_the_plan_hook_checks_the_resources_the_emitter_places(monkeypatch):
    """A place only the emitter derives is refused at stage 6, as at stage 7."""
    from fluid_build.iac.providers.gcp import GcpIacPlugin

    real_emit = GcpIacPlugin.emit

    def emit_with_a_key_ring(self, contract, actions=(), **kwargs):
        resources = real_emit(self, contract, actions, **kwargs)
        resources["google_kms_key_ring"] = {"k": {"name": "ring", "location": "asia"}}
        return resources

    monkeypatch.setattr(GcpIacPlugin, "emit", emit_with_a_key_ring)
    contract = _contract(dict(_BQ, region="europe-west1"))
    errors = _hook(contract)
    assert errors and all(e.startswith("google_kms_key_ring.k: ") for e in errors)
    assert any("Region 'asia' not in allowed regions list" in e for e in errors)


@pytest.mark.parametrize(
    "location, mode",
    [
        (dict(_BQ), "strict"),
        (dict(_BQ, region="us-central1"), "strict"),
        (dict(_BQ, location="us-central1"), "strict"),
        (dict(_BQ, region="europe-west1"), "strict"),
        (dict(_BQ), "advisory"),
        (dict(_BQ, region="us-central1"), "audit"),
    ],
)
def test_the_plan_hook_refuses_exactly_what_the_emitter_refuses(location, mode):
    contract = _contract(location, mode=mode)
    try:
        _emit(contract)
        refused = False
    except SovereigntyViolationError:
        refused = True
    assert bool(_hook(contract)) is refused


def test_the_plan_hook_gives_no_verdict_without_a_policy():
    assert _hook(_contract(dict(_BQ), sovereignty=False)) is None


def test_plan_check_sovereignty_reports_the_gcp_hook(workspace, capsys):
    contract = _multi_region_contract("EU")
    path = _write(workspace, contract)
    rc = _cli("plan", str(path), "--out", str(workspace / "plan.json"), "--check-sovereignty")
    out = capsys.readouterr().out
    assert rc == 0
    assert "Sovereignty check: PASS  — source: gcp provider hook" in out
