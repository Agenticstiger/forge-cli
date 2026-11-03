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

"""The operator's boundary around a managed Flink contract.

The contract names its kube context, namespace, image, service account,
mounted Secrets, its catalog and the variable holding the catalog token. None
of that may reach a cluster, a network or the environment unless the operator
listed it. These tests pin each refusal and that it comes first.
"""

from unittest.mock import patch

import pytest

from fluid_build.credentials.protected_env import is_protected_env_key
from fluid_build.streaming.compiler import compile_contract
from fluid_build.streaming.policy import (
    HOST_ALLOWLIST_ENV,
    IMAGE_ALLOWLIST_ENV,
    NAMESPACE_ALLOWLIST_ENV,
    SECRET_ALLOWLIST_ENV,
    SECRET_REF_ALLOWLIST_ENV,
    SERVICE_ACCOUNT_ALLOWLIST_ENV,
    FlinkPolicyError,
    catalog_request_guard,
    deployment_refusals,
    refuse_unless_catalog_host_allowed,
    refuse_unless_deployment_allowed,
)
from fluid_build.streaming.runtime import (
    Kubectl,
    catalog_evidence,
    inspect_drift,
    reconcile,
    verify,
)
from tests.streaming.conftest import EXAMPLE_ALLOWLISTS

DIGEST = "@sha256:" + "0" * 64


def _deployment(contract):
    return contract["builds"][0]["properties"]["parameters"]["flink"]["deployment"]


@pytest.fixture
def no_allowlists(monkeypatch):
    for name in EXAMPLE_ALLOWLISTS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("entry_point", ["apply", "dry-run", "diff", "verify"])
def test_an_unconfigured_operator_refuses_every_target_before_kubectl(
    contract, no_allowlists, entry_point
):
    with patch.object(Kubectl, "command", side_effect=AssertionError("kubectl ran")) as command:
        with pytest.raises(FlinkPolicyError) as refused:
            if entry_point == "diff":
                inspect_drift(contract)
            elif entry_point == "verify":
                verify(contract, timeout_seconds=0)
            else:
                reconcile(contract, dry_run=entry_point == "dry-run")
    command.assert_not_called()
    for name, value in EXAMPLE_ALLOWLISTS.items():
        assert name in refused.value.fix
    assert "5 target(s)" in refused.value.what
    assert "development" in refused.value.why and "flink-storage" in refused.value.why


@pytest.mark.parametrize("name", sorted(EXAMPLE_ALLOWLISTS))
def test_each_target_is_checked_on_its_own(contract, monkeypatch, name):
    monkeypatch.delenv(name)
    assert [variable for variable, _ in deployment_refusals(_deployment(contract))] == [name]


@pytest.mark.parametrize(
    "entry,image,allowed",
    [
        ("ghcr.io/acme/", "ghcr.io/acme/flink" + DIGEST, True),
        ("ghcr.io/acme/flink", "ghcr.io/acme/flink" + DIGEST, True),
        ("ghcr.io/acme", "ghcr.io/acmeevil/flink" + DIGEST, False),
        ("ghcr.io/acme", "ghcr.io/acme/flink" + DIGEST, False),
        ("ghcr.io/acme/flink", "ghcr.io/acme/flink-evil" + DIGEST, False),
    ],
)
def test_an_image_entry_is_a_repository_or_a_prefix_ending_in_slash(
    contract, monkeypatch, entry, image, allowed
):
    # A bare prefix is the bypass the Gatekeeper library warns about for K8sAllowedRepos.
    monkeypatch.setenv(IMAGE_ALLOWLIST_ENV, entry)
    deployment = {**_deployment(contract), "image": image}
    refusals = [v for v, _ in deployment_refusals(deployment)]
    assert (IMAGE_ALLOWLIST_ENV not in refusals) is allowed


def test_service_account_and_secret_entries_can_be_scoped_to_a_namespace(contract, monkeypatch):
    deployment = _deployment(contract)
    monkeypatch.setenv(SERVICE_ACCOUNT_ALLOWLIST_ENV, "other/flink")
    monkeypatch.setenv(SECRET_ALLOWLIST_ENV, "flink-storage")  # any allowed namespace
    assert deployment_refusals(deployment) == [
        (SERVICE_ACCOUNT_ALLOWLIST_ENV, "data-products/flink")
    ]
    assert deployment_refusals({**deployment, "secretRefs": []}) == [
        (SERVICE_ACCOUNT_ALLOWLIST_ENV, "data-products/flink")
    ]


def test_the_fix_keeps_what_the_operator_already_listed(contract, monkeypatch):
    monkeypatch.setenv(NAMESPACE_ALLOWLIST_ENV, "analytics")
    with pytest.raises(FlinkPolicyError) as refused:
        refuse_unless_deployment_allowed("ingest-orders", _deployment(contract))
    assert f'{NAMESPACE_ALLOWLIST_ENV}="analytics,data-products"' in refused.value.fix


@pytest.mark.parametrize(
    "name", [*EXAMPLE_ALLOWLISTS, HOST_ALLOWLIST_ENV, SECRET_REF_ALLOWLIST_ENV]
)
def test_a_project_dotenv_cannot_grant_these(name):
    assert is_protected_env_key(name)


@pytest.mark.parametrize(
    "url,allowlist,refused",
    [
        ("http://169.254.169.254/", "169.254.169.254", True),  # metadata: never
        ("http://[fe80::1]/", "", True),
        ("https://8.8.8.8/", "", True),  # public: only when listed
        ("https://8.8.8.8/", "8.8.8.8", False),
        ("http://127.0.0.1:8181", "", False),  # loopback and private need no entry
        ("http://10.0.0.5:8181", "", False),
    ],
)
def test_the_catalog_host_follows_the_deployment_host_policy(monkeypatch, url, allowlist, refused):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, allowlist)
    if refused:
        with pytest.raises(FlinkPolicyError):
            refuse_unless_catalog_host_allowed(url, role="catalog")
        with pytest.raises(FlinkPolicyError):
            catalog_request_guard(url, role="catalog")(object())
    else:
        refuse_unless_catalog_host_allowed(url, role="catalog")


@pytest.mark.parametrize("auth_type", ["bearer", "oauth2"])
def test_the_token_variable_needs_an_entry_for_its_host_before_it_is_read(
    contract, monkeypatch, auth_type
):
    contract["exposes"][0]["binding"]["location"]["uri"] = "https://8.8.8.8"
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "8.8.8.8,9.9.9.9")
    cfg = contract["builds"][0]["properties"]["parameters"]["flink"]
    if auth_type == "bearer":
        cfg["catalogAuth"] = {"type": "bearer", "tokenEnv": "CI_SECRET"}
        expected = "CI_SECRET@8.8.8.8"
    else:
        cfg["catalogAuth"] = {
            "type": "oauth2",
            "credentialEnv": "CI_SECRET",
            "oauth2ServerUri": "https://9.9.9.9/token",
            "scope": "catalog",
        }
        expected = "CI_SECRET@9.9.9.9"
    monkeypatch.setenv("CI_SECRET", "sentinel-value-must-not-leak")
    (artifact,) = compile_contract(contract)
    with patch(
        "fluid_build.streaming.runtime.safe_httpx_client",
        side_effect=AssertionError("a request was built"),
    ):
        with pytest.raises(FlinkPolicyError) as refused:
            catalog_evidence(contract, contract["builds"][0], artifact)
    assert f'{SECRET_REF_ALLOWLIST_ENV}="{expected}"' in refused.value.fix
    assert "sentinel-value-must-not-leak" not in str(refused.value.as_json())


def test_a_link_local_token_endpoint_is_refused(contract, monkeypatch):
    cfg = contract["builds"][0]["properties"]["parameters"]["flink"]
    contract["exposes"][0]["binding"]["location"]["uri"] = "https://10.0.0.5"
    cfg["catalogAuth"] = {
        "type": "oauth2",
        "credentialEnv": "CI_SECRET",
        "oauth2ServerUri": "https://169.254.169.254/token",
        "scope": "catalog",
    }
    monkeypatch.setenv(SECRET_REF_ALLOWLIST_ENV, "CI_SECRET")
    (artifact,) = compile_contract(contract)
    with pytest.raises(FlinkPolicyError) as refused:
        catalog_evidence(contract, contract["builds"][0], artifact)
    assert "link-local" in refused.value.why
