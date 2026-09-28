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

"""The default remote state key names the provider.

Measured on 0.16.5 with ``FLUID_STATE_BACKEND=s3://fluid-demo-lab-state-…``:
``resolve_state_target`` gave the aws and the gcp apply of
``bronze.customer_subscriptions`` the same key,
``fluid/bronze.customer_subscriptions/terraform.tfstate``, so the gcp plan
would have read the aws resources as orphans to destroy. Offline unit pins
for the key, the old key the move reads from, and how a state is attributed
to a provider; ``test_iac_state_key_migration_moto.py`` runs the move itself
with real ``tofu``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from fluid_build.cli._apply_opentofu_engine import resolve_state_target
from fluid_build.iac import state_migration as mig
from fluid_build.iac.backend import default_state_key, legacy_default_backend, parse_backend

pytestmark = pytest.mark.unit

_CID = "bronze.customer_subscriptions"
_CONTRACT = {"id": _CID, "name": "Customer Subscriptions"}
_PACKAGED = {"id": _CID, "packaging": {"mode": "isolated"}}


def _args(tmp_path: Path, flag=None) -> argparse.Namespace:
    return argparse.Namespace(state_backend=flag, workspace_dir=str(tmp_path))


def test_the_aws_and_the_gcp_apply_of_one_contract_get_two_states(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUID_STATE_BACKEND", "s3://fluid-demo-lab-state-111111111111")
    aws = resolve_state_target(_args(tmp_path), _CONTRACT, "aws")
    gcp = resolve_state_target(_args(tmp_path), _CONTRACT, "gcp")
    assert aws.backend == {
        "s3": {
            "bucket": "fluid-demo-lab-state-111111111111",
            "key": f"fluid/{_CID}/aws/terraform.tfstate",
        }
    }
    assert gcp.backend["s3"]["key"] == f"fluid/{_CID}/gcp/terraform.tfstate"
    # ...and both know where the previous release kept the state.
    legacy = {
        "s3": {
            "bucket": "fluid-demo-lab-state-111111111111",
            "key": f"fluid/{_CID}/terraform.tfstate",
        }
    }
    assert aws.legacy_backend == legacy
    assert gcp.legacy_backend == legacy


def test_a_gcs_prefix_names_the_provider_too(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUID_STATE_BACKEND", "gcs://team-state")
    target = resolve_state_target(_args(tmp_path), _CONTRACT, "gcp")
    assert target.backend == {"gcs": {"bucket": "team-state", "prefix": f"fluid/{_CID}/gcp"}}
    assert target.legacy_backend == {"gcs": {"bucket": "team-state", "prefix": f"fluid/{_CID}"}}


def test_a_packaging_contract_on_the_flag_gets_the_provider_segment(tmp_path, monkeypatch):
    monkeypatch.delenv("FLUID_STATE_BACKEND", raising=False)
    target = resolve_state_target(_args(tmp_path, "s3://ci-state"), _PACKAGED, "aws")
    assert (
        target.backend["s3"]["key"] == "fluid/bronze_customer_subscriptions/aws/terraform.tfstate"
    )
    assert target.legacy_backend["s3"]["key"] == (
        "fluid/bronze_customer_subscriptions/terraform.tfstate"
    )


@pytest.mark.parametrize(
    "flag",
    ["s3://ci-state", "s3://ci-state/team/explicit.tfstate", "gcs://ci-state/team/x", ""],
    ids=["legacy-shared-key", "explicit-key", "explicit-prefix", "local"],
)
def test_keys_nobody_defaulted_are_never_moved(tmp_path, monkeypatch, flag):
    """The shared legacy key, an explicit key or prefix and local state keep
    their location, so there is nothing to migrate from."""
    monkeypatch.delenv("FLUID_STATE_BACKEND", raising=False)
    target = resolve_state_target(_args(tmp_path, flag), _CONTRACT, "aws")
    assert target.legacy_backend is None
    if flag == "s3://ci-state":
        assert target.backend == {"s3": {"bucket": "ci-state", "key": "fluid/terraform.tfstate"}}


def test_parse_backend_without_a_provider_is_unchanged():
    """Callers that do not name a provider keep the old keys byte for byte."""
    assert parse_backend("s3://b", _CONTRACT, per_contract_default=True) == {
        "s3": {"bucket": "b", "key": f"fluid/{_CID}/terraform.tfstate"}
    }
    assert default_state_key(_CONTRACT) == "fluid/terraform.tfstate"


def test_a_provider_that_is_not_one_key_segment_is_refused():
    with pytest.raises(ValueError, match="cannot name a state key segment"):
        default_state_key(_CONTRACT, per_contract=True, provider="../aws")
    with pytest.raises(ValueError):
        legacy_default_backend("s3://b", _CONTRACT, per_contract_default=True, provider="a/b")


# ── Whose state is it? ────────────────────────────────────────────────────


def _res(source: str, rtype: str = "x") -> dict:
    return {"type": rtype, "provider": f'provider["registry.opentofu.org/{source}"]'}


@pytest.mark.parametrize(
    "resources, provider, verdict",
    [
        ([_res("hashicorp/aws"), _res("hashicorp/null")], "aws", "mine"),
        ([_res("hashicorp/google")], "gcp", "mine"),
        ([_res("hashicorp/aws")], "gcp", "other"),
        ([_res("hashicorp/google")], "aws", "other"),
        ([_res("hashicorp/aws"), _res("hashicorp/google")], "aws", "ambiguous"),
        ([_res("hashicorp/aws"), _res("hashicorp/google")], "gcp", "ambiguous"),
        ([_res("hashicorp/random")], "aws", "ambiguous"),
        ([{"type": "x", "provider": "not an address"}], "aws", "ambiguous"),
        ([_res("snowflake-labs/snowflake")], "snowflake", "mine"),
    ],
    ids=[
        "aws-own",
        "gcp-own",
        "aws-state-seen-by-gcp",
        "gcp-state-seen-by-aws",
        "two-clouds-aws",
        "two-clouds-gcp",
        "unknown-provider",
        "unreadable-address",
        "snowflake-pre-v2-source",
    ],
)
def test_a_state_is_attributed_by_its_resources_providers(resources, provider, verdict):
    assert mig.classify(resources, provider)[0] == verdict


def test_a_terraform_written_state_is_read_the_same():
    resources = [{"provider": 'provider["registry.terraform.io/hashicorp/aws"].west'}]
    assert mig.classify(resources, "aws")[0] == "mine"


def test_state_pull_output_is_parsed_and_an_absent_state_is_empty():
    assert not mig.parse_state("").exists
    empty = '{"version":4,"serial":0,"lineage":"","resources":[]}'
    assert not mig.parse_state(empty).exists
    doc = mig.parse_state(json.dumps({"lineage": "L", "serial": 3, "resources": [_res("a/b")]}))
    assert (doc.exists, doc.serial, len(doc.resources)) == (True, 3, 1)
    with pytest.raises(mig.StateMigrationError):
        mig.parse_state("not json")


def test_the_workdir_s_recorded_backend_is_compared_on_the_fields_forge_writes(tmp_path):
    (tmp_path / ".terraform").mkdir()
    (tmp_path / ".terraform" / "terraform.tfstate").write_text(
        json.dumps(
            {
                "backend": {
                    "type": "s3",
                    "config": {"bucket": "b", "key": "fluid/x/terraform.tfstate", "region": None},
                }
            }
        ),
        encoding="utf-8",
    )
    assert mig.records_backend(
        tmp_path, {"s3": {"bucket": "b", "key": "fluid/x/terraform.tfstate"}}
    )
    assert not mig.records_backend(
        tmp_path, {"s3": {"bucket": "b", "key": "fluid/x/aws/terraform.tfstate"}}
    )
    assert not mig.records_backend(tmp_path / "nowhere", {"s3": {"bucket": "b"}})
