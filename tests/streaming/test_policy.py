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

from types import SimpleNamespace
from unittest.mock import patch

import httpx
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
    refuse_unless_catalog_secret_allowed,
    refuse_unless_catalog_targets_allowed,
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
        ("ghcr.io/acme/flink", "ghcr.io/acme/flink:1.20" + DIGEST, True),  # the digest decides
        ("localhost:5000/flink", "localhost:5000/flink:1.20" + DIGEST, True),
        ("localhost", "localhost:5000/flink" + DIGEST, False),  # a registry port is no tag
        ("forge-flink", "forge-flink@x/y" + DIGEST, False),  # no image kubelet runs has two "@"
        ("forge-flink", "forge-flink:1@x" + DIGEST, False),
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


def _sink(contract, uri, auth=None):
    """Point the sink at catalog ``uri``; with ``auth``, give the build that catalogAuth."""
    contract["exposes"][0]["binding"]["location"]["uri"] = uri
    if auth is not None:
        contract["builds"][0]["properties"]["parameters"]["flink"]["catalogAuth"] = auth
    return contract


def _oauth2(token_endpoint):
    return {
        "type": "oauth2",
        "credentialEnv": "CI_SECRET",
        "oauth2ServerUri": token_endpoint,
        "scope": "catalog",
    }


def _refused_before_any_client(contract):
    """Run verify's catalog step, failing the test if any HTTP client is built."""
    (artifact,) = compile_contract(contract)
    with patch(
        "fluid_build.streaming.runtime.safe_httpx_client",
        side_effect=AssertionError("a client was built"),
    ):
        with pytest.raises(FlinkPolicyError) as refused:
            catalog_evidence(contract, contract["builds"][0], artifact)
    return refused.value


@pytest.mark.parametrize("auth_type", ["bearer", "oauth2"])
def test_the_token_variable_needs_an_entry_for_its_host_before_it_is_read(
    contract, monkeypatch, auth_type
):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "8.8.8.8,9.9.9.9")
    if auth_type == "bearer":
        _sink(contract, "https://8.8.8.8", {"type": "bearer", "tokenEnv": "CI_SECRET"})
        expected = "CI_SECRET@8.8.8.8"
    else:
        _sink(contract, "https://8.8.8.8", _oauth2("https://9.9.9.9/token"))
        expected = "CI_SECRET@9.9.9.9"
    monkeypatch.setenv("CI_SECRET", "sentinel-value-must-not-leak")
    refused = _refused_before_any_client(contract)
    assert f'{SECRET_REF_ALLOWLIST_ENV}="{expected}"' in refused.fix
    assert "sentinel-value-must-not-leak" not in str(refused.as_json())


def test_a_link_local_token_endpoint_is_refused(contract, monkeypatch):
    monkeypatch.setenv(SECRET_REF_ALLOWLIST_ENV, "CI_SECRET")
    _sink(contract, "https://10.0.0.5", _oauth2("https://169.254.169.254/token"))
    assert "link-local" in _refused_before_any_client(contract).why


def _pinned(ip):
    """A request as the guard sees it: safe_httpx_client's pin hook has put the IP in."""
    return SimpleNamespace(url=SimpleNamespace(host=ip))


@pytest.mark.parametrize(
    "url,pinned,allowlist,refused",
    [
        # The name passes (unresolved here, private for an IDNA2003 lookup of faß.de),
        # yet the connection is pinned to a public address: refused.
        ("https://catalog.internal/", "93.184.216.34", "", True),
        ("https://faß.de/", "93.184.216.34", "", True),
        ("https://catalog.internal/", "10.0.0.5", "", False),
        # A listed name may connect to its public address, never to metadata.
        ("https://catalog.example.com/", "93.184.216.34", "catalog.example.com", False),
        ("https://catalog.example.com/", "169.254.169.254", "catalog.example.com", True),
    ],
)
def test_the_request_guard_judges_the_pinned_address_not_a_second_lookup(
    monkeypatch, url, pinned, allowlist, refused
):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, allowlist)
    guard = catalog_request_guard(url, role="Iceberg catalog")
    with patch(
        "fluid_build.build_runners._deployment_host_policy.hostname_scope",
        side_effect=lambda host: (
            "unresolved"
            if host in ("catalog.internal", "faß.de", "catalog.example.com")
            else __import__("fluid_build._net", fromlist=["x"]).hostname_scope(host)
        ),
    ):
        if refused:
            with pytest.raises(FlinkPolicyError):
                guard(_pinned(pinned))
        else:
            guard(_pinned(pinned))


def test_an_oauth2_token_goes_only_to_a_catalog_paired_with_the_credential(contract, monkeypatch):
    # The credential is listed for its identity provider; the contract keeps that IdP
    # and points the catalog elsewhere, so the token minted from it would go there.
    monkeypatch.setenv(SECRET_REF_ALLOWLIST_ENV, "CI_SECRET@10.0.0.5")
    monkeypatch.setenv("CI_SECRET", "id:secret")
    _sink(contract, "https://10.0.0.9", _oauth2("https://10.0.0.5/token"))
    assert "CI_SECRET@10.0.0.9" in _refused_before_any_client(contract).fix


@pytest.mark.parametrize("dry_run", [True, False])
def test_apply_checks_the_jobs_catalog_destinations_before_kubectl(contract, dry_run):
    # The job's CREATE CATALOG sends env:<NAME> from the pod, whose mounted Secrets
    # hold more than the catalog token: pairing the variable with the host is the gate.
    auth = {"type": "bearer", "tokenEnv": "AWS_SECRET_ACCESS_KEY"}
    _sink(contract, "https://iceberg-rest:8181", auth)
    with patch.object(Kubectl, "command", side_effect=AssertionError("kubectl ran")) as command:
        with pytest.raises(FlinkPolicyError) as refused:
            reconcile(contract, dry_run=dry_run)
    command.assert_not_called()
    assert "AWS_SECRET_ACCESS_KEY@iceberg-rest" in refused.value.fix


def test_apply_refuses_a_link_local_s3_endpoint(contract):
    cfg = contract["builds"][0]["properties"]["parameters"]["flink"]
    cfg["catalogProperties"] = {"s3.endpoint": "http://169.254.169.254"}
    with pytest.raises(FlinkPolicyError) as refused:
        reconcile(contract, dry_run=True)
    assert "S3 endpoint" in refused.value.what


@pytest.mark.parametrize(
    "uri", ["http://169.254.169.254", "https://8.8.8.8", "http://[fe80::1]:8181"]
)
def test_verify_refuses_the_catalog_host_before_any_client_is_built(contract, monkeypatch, uri):
    monkeypatch.delenv(HOST_ALLOWLIST_ENV, raising=False)
    assert "Iceberg catalog" in _refused_before_any_client(_sink(contract, uri)).what


def test_both_catalog_clients_carry_the_pinned_address_guard(contract, monkeypatch):
    monkeypatch.setenv(SECRET_REF_ALLOWLIST_ENV, "CI_SECRET@10.0.0.5,CI_SECRET@10.0.0.9")
    monkeypatch.setenv("CI_SECRET", "id:secret")
    _sink(contract, "https://10.0.0.9", _oauth2("https://10.0.0.5/token"))
    (artifact,) = compile_contract(contract)
    guards = []

    def client(**kwargs):
        guards.append(kwargs["event_hooks"]["request"][-1])
        if len(guards) == 2:
            raise LookupError("both clients built")  # stop before the catalog is called
        token = httpx.Response(200, json={"access_token": "minted"})
        return httpx.Client(transport=httpx.MockTransport(lambda request: token))

    with patch("fluid_build.streaming.runtime.safe_httpx_client", side_effect=client):
        with pytest.raises(LookupError):
            catalog_evidence(contract, contract["builds"][0], artifact)
    for guard, role in zip(guards, ["OAuth2 token endpoint", "Iceberg catalog"], strict=True):
        guard(_pinned("10.0.0.5"))  # a private address needs no entry
        with pytest.raises(FlinkPolicyError) as refused:
            guard(_pinned("169.254.169.254"))
        assert role in refused.value.what


def test_a_pinned_refusal_names_the_host_to_list_and_the_address_it_reached(monkeypatch):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "corp.example")
    guard = catalog_request_guard("https://catalog.internal/", role="Iceberg catalog")
    with patch(
        "fluid_build.build_runners._deployment_host_policy.hostname_scope",
        side_effect=lambda host: "unresolved" if host == "catalog.internal" else "public",
    ):
        with pytest.raises(FlinkPolicyError) as refused:
            guard(_pinned("93.184.216.34"))
    assert "'catalog.internal', connecting to '93.184.216.34'," in refused.value.why
    assert f'export {HOST_ALLOWLIST_ENV}="corp.example,catalog.internal"' in refused.value.fix


def test_a_malformed_catalog_url_is_refused_not_raised():
    with pytest.raises(FlinkPolicyError) as refused:
        refuse_unless_catalog_host_allowed("http://[::1", role="Iceberg catalog")
    assert "no host" in refused.value.why


@pytest.mark.parametrize("uri", ["http://localhost:8181", "http://127.0.0.1:8181"])
def test_a_loopback_catalog_needs_no_entry_for_its_host_or_its_token(uri):
    cfg = {"catalogAuth": {"type": "bearer", "tokenEnv": "CI_SECRET"}}
    auth = refuse_unless_catalog_targets_allowed(cfg, uri, environ={})
    assert auth is not None and auth.env_var == "CI_SECRET"


def test_the_token_fix_keeps_valid_entries_and_drops_ignored_ones(monkeypatch):
    monkeypatch.setenv(SECRET_REF_ALLOWLIST_ENV, "TOK@,OTHER@corp.example")
    with pytest.raises(FlinkPolicyError) as refused:
        refuse_unless_catalog_secret_allowed("CI_SECRET", "https://catalog.example/")
    expected = "OTHER@corp.example,CI_SECRET@catalog.example"
    assert f'export {SECRET_REF_ALLOWLIST_ENV}="{expected}"' in refused.value.fix
