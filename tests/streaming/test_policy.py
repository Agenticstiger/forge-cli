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

import copy
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
    refuse_unless_source_hosts_allowed,
)
from fluid_build.streaming.runtime import (
    Kubectl,
    StreamingError,
    _oauth2_access_token,
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
        # The job resolves a name itself, later: a name needs an entry, whatever DNS says here.
        ("http://iceberg-rest:8181", "", True),
        ("http://iceberg-rest:8181", "iceberg-rest", False),
        ("http://minio.lake.svc.cluster.local:9000", "svc.cluster.local", False),
        ("http://localhost:8181", "", False),
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


@pytest.mark.parametrize("ip", ["100.100.100.200", "[fd00:ec2::254]"])
def test_a_metadata_token_endpoint_is_refused_by_name(contract, monkeypatch, ip):
    """A metadata endpoint outside link-local is refused, and the reason says what it is."""
    monkeypatch.setenv(SECRET_REF_ALLOWLIST_ENV, "CI_SECRET")
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, ip.strip("[]"))
    _sink(contract, "https://10.0.0.5", _oauth2(f"https://{ip}/token"))
    assert "known cloud metadata endpoint" in _refused_before_any_client(contract).why


def _pinned(ip):
    """A request as the guard sees it: safe_httpx_client's pin hook has put the IP in."""
    return SimpleNamespace(url=SimpleNamespace(host=ip))


@pytest.mark.parametrize(
    "url,pinned,allowlist,refused",
    [
        # An unlisted name is refused, whatever a lookup here or the pin says.
        ("https://catalog.internal/", "93.184.216.34", "", True),
        ("https://faß.de/", "93.184.216.34", "", True),
        ("https://catalog.internal/", "10.0.0.5", "catalog.internal", False),
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


def test_a_pinned_refusal_names_the_host_and_the_address_it_reached(monkeypatch):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "catalog.example.com")
    guard = catalog_request_guard("https://catalog.example.com/", role="Iceberg catalog")
    with pytest.raises(FlinkPolicyError) as refused:
        guard(_pinned("169.254.169.254"))
    assert "'catalog.example.com', connecting to '169.254.169.254'," in refused.value.why


@pytest.mark.parametrize("scope", ["unresolved", "non_public"])
def test_a_name_the_job_resolves_needs_an_entry_whatever_it_resolves_to_here(
    contract, monkeypatch, scope
):
    # DNS here at apply proves nothing about where the pod connects later.
    cfg = contract["builds"][0]["properties"]["parameters"]["flink"]
    cfg["catalogProperties"] = {"s3.endpoint": "https://s3.attacker.example"}
    with (
        patch(
            "fluid_build.build_runners._deployment_host_policy.hostname_scope",
            return_value=scope,
        ),
        patch.object(Kubectl, "command", side_effect=AssertionError("kubectl ran")),
    ):
        with pytest.raises(FlinkPolicyError) as refused:
            reconcile(contract, dry_run=True)
    assert "S3 endpoint" in refused.value.what
    assert "resolves it itself" in refused.value.why
    listed = "iceberg-rest,kafka,s3.attacker.example"  # the operator's entry is kept
    assert f'export {HOST_ALLOWLIST_ENV}="{listed}"' in refused.value.fix


def test_a_malformed_catalog_url_is_refused_not_raised():
    with pytest.raises(FlinkPolicyError) as refused:
        refuse_unless_catalog_host_allowed("http://[::1", role="Iceberg catalog")
    assert "no host" in refused.value.why


@pytest.mark.parametrize("uri", ["http://localhost:8181", "http://127.0.0.1:8181"])
def test_a_loopback_catalog_needs_no_entry_for_its_host_or_its_token(uri):
    cfg = {"catalogAuth": {"type": "bearer", "tokenEnv": "CI_SECRET"}}
    auth = refuse_unless_catalog_targets_allowed(cfg, uri, environ={})
    assert auth is not None and auth.env_var == "CI_SECRET"


@pytest.mark.parametrize(
    "field,value",
    [
        ("catalogProperties", "env:AWS_SECRET_ACCESS_KEY"),
        ("catalogProperties", "${AWS_SECRET_ACCESS_KEY}"),
        ("location", "env:AWS_SECRET_ACCESS_KEY"),
        ("catalogProperties", "EU-WEST-1"),
    ],
)
def test_contract_text_cannot_make_the_pod_send_one_of_its_variables(contract, field, value):
    # Iceberg resolves env:<NAME> in any catalog property inside the pod, and the
    # region is signed into every S3 request: only catalogAuth may name a variable.
    if field == "location":
        contract["exposes"][0]["binding"]["location"]["region"] = value
    else:
        managed = contract["builds"][0]["properties"]["parameters"]["flink"]
        managed["catalogProperties"] = {"client.region": value}
    with pytest.raises(ValueError, match="client.region"):
        compile_contract(contract)


def test_contract_sql_cannot_override_the_source_connector(contract):
    # An OPTIONS hint would repoint the Kafka source, or wire a config provider that
    # reads the pod's secrets, at query time.
    contract["builds"][0]["properties"]["sql"] = (
        "SELECT event_id FROM source_events "
        "/*+ OPTIONS('properties.bootstrap.servers'='attacker.example:9092') */"
    )
    with pytest.raises(ValueError, match="SQL hints"):
        compile_contract(contract)


def test_a_region_name_still_reaches_the_catalog(contract):
    contract["exposes"][0]["binding"]["location"]["region"] = "eu-central-1"
    (artifact,) = compile_contract(contract)
    assert "'client.region' = 'eu-central-1'" in artifact.statements[0]


def test_an_oauth2_token_endpoint_must_use_https(contract):
    _sink(contract, "https://10.0.0.9", _oauth2("http://10.0.0.8/token"))
    with pytest.raises(ValueError, match="OAuth2 token endpoint requires HTTPS"):
        compile_contract(contract)
    auth = SimpleNamespace(
        oauth2_server_uri="http://10.0.0.8/token", scope="catalog", audience=None, resource=None
    )
    with patch(
        "fluid_build.streaming.runtime.safe_httpx_client",
        side_effect=AssertionError("a client was built"),
    ):
        with pytest.raises(StreamingError, match="must use https"):
            _oauth2_access_token(auth, "id:secret")


def test_the_token_fix_keeps_valid_entries_and_drops_ignored_ones(monkeypatch):
    monkeypatch.setenv(SECRET_REF_ALLOWLIST_ENV, "TOK@,OTHER@corp.example")
    with pytest.raises(FlinkPolicyError) as refused:
        refuse_unless_catalog_secret_allowed("CI_SECRET", "https://catalog.example/")
    expected = "OTHER@corp.example,CI_SECRET@catalog.example"
    assert f'export {SECRET_REF_ALLOWLIST_ENV}="{expected}"' in refused.value.fix


# ── The Kafka brokers and the Schema Registry, by the catalog's rule ─────────


def _source(contract):
    return contract["builds"][0]["properties"]["parameters"]["flink"]["source"]


def _brokers(contract, servers):
    _source(contract)["bootstrapServers"] = servers
    return contract


def _refused_before_kubectl(contract, **kwargs):
    with patch.object(Kubectl, "command", side_effect=AssertionError("kubectl ran")):
        with pytest.raises(FlinkPolicyError) as refused:
            reconcile(contract, **kwargs)
    return refused.value


@pytest.mark.parametrize("dry_run", [True, False])
def test_an_unlisted_broker_name_is_refused_before_any_kubectl_call(contract, monkeypatch, dry_run):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "iceberg-rest")
    refused = _refused_before_kubectl(contract, dry_run=dry_run)
    assert "Kafka bootstrap server" in refused.what
    assert "host 'kafka' is a name the operator has not listed" in refused.why
    assert f'export {HOST_ALLOWLIST_ENV}="iceberg-rest,kafka"' in refused.fix


def test_a_second_broker_that_is_not_listed_is_refused_too(contract, monkeypatch):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "iceberg-rest,kafka")
    _brokers(contract, "kafka:9092, other.example:9092")
    assert "'other.example'" in _refused_before_kubectl(contract, dry_run=True).why


def test_a_broker_the_operator_lists_passes(contract):
    _brokers(contract, "kafka:9092,kafka:9093")
    with patch.object(Kubectl, "command", side_effect=AssertionError("kubectl ran")):
        (result,) = reconcile(contract, dry_run=True)
    assert result["status"] == "planned"


def test_every_build_clears_the_policy_before_any_build_is_read(contract, monkeypatch):
    """The second build's refusal must not follow a kubectl read of the first."""
    second = copy.deepcopy(contract["builds"][0])
    second["id"] = "other"
    second["outputs"] = ["other-output"]
    flink = second["properties"]["parameters"]["flink"]
    flink["source"]["bootstrapServers"] = "unlisted.example:9092"
    flink["deployment"].update(
        name="other",
        checkpointUri="s3://flink-state/other/checkpoints",
        savepointUri="s3://flink-state/other/savepoints",
        haUri="s3://flink-state/other/ha",
    )
    contract["builds"].append(second)
    target = copy.deepcopy(contract["exposes"][0])
    target["exposeId"] = "other-output"
    target["binding"]["location"]["table"] = "other_table"
    contract["exposes"].append(target)
    assert "'unlisted.example'" in _refused_before_kubectl(contract).why


@pytest.mark.parametrize("servers", ["10.0.0.5:9092", "127.0.0.1:9092", "localhost:9092"])
def test_a_private_or_loopback_broker_needs_no_entry(contract, monkeypatch, servers):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "iceberg-rest")
    reconcile(_brokers(contract, servers), dry_run=True)


def test_a_public_ip_broker_needs_an_entry_and_a_link_local_one_is_never_allowed(
    contract, monkeypatch
):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "iceberg-rest")
    refused = _refused_before_kubectl(_brokers(contract, "8.8.8.8:9092"), dry_run=True)
    assert "public and not in" in refused.why
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "iceberg-rest,8.8.8.8,169.254.169.254")
    reconcile(_brokers(contract, "8.8.8.8:9092"), dry_run=True)
    refused = _refused_before_kubectl(_brokers(contract, "169.254.169.254:9092"), dry_run=True)
    assert "link-local" in refused.why and "Kafka bootstrap server" in refused.what


@pytest.mark.parametrize(
    "servers,verdict",
    [
        ("[::1]:9092", "passes"),
        ("[fe80::1]:9092", "link-local"),
        ("[2001:4860:4860::8888]:9092", "public"),
        ("kafka:9092,[::1]:9092", "passes"),
    ],
)
def test_a_bracketed_ipv6_broker_is_parsed_and_judged(contract, monkeypatch, servers, verdict):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "iceberg-rest,kafka")
    _brokers(contract, servers)
    if verdict == "passes":
        reconcile(contract, dry_run=True)
    else:
        assert verdict in _refused_before_kubectl(contract, dry_run=True).why


@pytest.mark.parametrize(
    "servers,reason",
    [
        ("kafka:9092,,kafka:9093", "entry 2 is empty"),
        ("kafka:9092,", "entry 2 is empty"),
        ("[::1:9092", "unbalanced bracket"),
        ("::1]:9092", "unbalanced bracket"),
        ("::1:9092", "IPv6 address without brackets"),
        ("kafka", "has no port"),
        ("kafka:", "no numeric port"),
        ("kafka:abc", "no numeric port"),
        ("kafka:70000", "outside 1-65535"),
        ("[kafka]:9092", "something other than an IPv6 address"),
        (":9092", "has no host"),
    ],
)
def test_a_malformed_broker_list_is_a_typed_refusal(contract, servers, reason):
    refused = _refused_before_kubectl(_brokers(contract, servers), dry_run=True)
    assert "Kafka bootstrap server" in refused.what
    assert reason in refused.why
    assert "host:port" in refused.fix


def _registry(contract, url):
    stream = contract["builds"][0]["properties"]["stream"]
    stream["format"] = "avro-confluent"
    _source(contract)["schemaRegistryUrl"] = url
    return contract


def test_an_unlisted_schema_registry_name_is_refused(contract, monkeypatch):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "iceberg-rest,kafka")
    refused = _refused_before_kubectl(_registry(contract, "http://registry:8081"), dry_run=True)
    assert "Schema Registry" in refused.what
    assert "host 'registry' is a name the operator has not listed" in refused.why


def test_a_listed_schema_registry_and_a_private_ip_one_pass(contract, monkeypatch):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "iceberg-rest,kafka,registry")
    reconcile(_registry(contract, "http://registry:8081"), dry_run=True)
    reconcile(_registry(contract, "http://10.0.0.7:8081"), dry_run=True)
    refused = _refused_before_kubectl(
        _registry(contract, "http://169.254.169.254:8081"), dry_run=True
    )
    assert "link-local" in refused.why and "Schema Registry" in refused.what


# ── A token sent from the pod: loopback is not the machine fluid runs on ─────


def _loopback_catalog(contract):
    return _sink(contract, "https://localhost:8181", {"type": "bearer", "tokenEnv": "CI_SECRET"})


def test_apply_refuses_a_loopback_token_destination_without_an_entry(contract, monkeypatch):
    monkeypatch.delenv(SECRET_REF_ALLOWLIST_ENV, raising=False)
    monkeypatch.setenv("CI_SECRET", "sentinel-value-must-not-leak")
    refused = _refused_before_kubectl(_loopback_catalog(contract), dry_run=True)
    assert "'CI_SECRET'" in refused.what and "'localhost'" in refused.what
    assert "pod itself or a sidecar" in refused.why
    assert f'{SECRET_REF_ALLOWLIST_ENV}="CI_SECRET@localhost"' in refused.fix
    assert "sentinel-value-must-not-leak" not in str(refused.as_json())


def test_apply_accepts_a_loopback_token_destination_the_operator_paired(contract, monkeypatch):
    monkeypatch.setenv(SECRET_REF_ALLOWLIST_ENV, "CI_SECRET@localhost")
    reconcile(_loopback_catalog(contract), dry_run=True)


def test_apply_refuses_a_loopback_oauth2_endpoint_without_an_entry(contract, monkeypatch):
    monkeypatch.setenv(SECRET_REF_ALLOWLIST_ENV, "CI_SECRET@catalog.example")
    _sink(contract, "https://catalog.example", _oauth2("https://localhost/token"))
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "iceberg-rest,kafka,catalog.example")
    refused = _refused_before_kubectl(contract, dry_run=True)
    assert "'localhost'" in refused.what


def test_verify_keeps_the_loopback_exemption_and_apply_does_not(monkeypatch):
    from fluid_build.streaming.runtime import _catalog_headers

    cfg = {"catalogAuth": {"type": "bearer", "tokenEnv": "CI_SECRET"}}
    monkeypatch.delenv(SECRET_REF_ALLOWLIST_ENV, raising=False)
    monkeypatch.setenv("CI_SECRET", "tok")
    assert _catalog_headers(cfg, "https://localhost:8181") == {"Authorization": "Bearer tok"}
    with pytest.raises(FlinkPolicyError):
        refuse_unless_catalog_targets_allowed(
            cfg, "https://localhost:8181", environ={}, pod_side=True
        )
    assert refuse_unless_catalog_targets_allowed(cfg, "https://localhost:8181", environ={})


@pytest.mark.parametrize(
    "servers",
    [
        "ok/evil.example:9092",
        "ok?x@evil.example:9092",
        "ok#x@evil.example:9092",
        "ok/path:9092",
        "user@ok:9092",
        "ok\\evil.example:9092",
    ],
)
def test_a_broker_entry_the_client_could_read_as_another_host_is_refused(servers):
    # Kafka's client takes the host after any '/', '?', '#' or '@': judging the
    # text before it would approve 'ok' while the job dials evil.example.
    cfg = {"source": {"bootstrapServers": servers}}
    with pytest.raises(FlinkPolicyError) as refused:
        refuse_unless_source_hosts_allowed(cfg, environ={HOST_ALLOWLIST_ENV: "ok"})
    assert "characters a host name cannot have" in refused.value.why


def test_a_registry_url_with_a_backslash_is_refused():
    cfg = {
        "source": {"bootstrapServers": "ok:9092", "schemaRegistryUrl": "http://evil.example\\.ok"}
    }
    with pytest.raises(FlinkPolicyError) as refused:
        refuse_unless_source_hosts_allowed(cfg, environ={HOST_ALLOWLIST_ENV: "ok"})
    assert "characters a host name cannot have" in refused.value.why


@pytest.mark.parametrize(
    "servers",
    ["ok:9092", "OK:9092", "kafka-0.ok:9092,kafka_1.ok:9093", "[::1]:9092", "10.0.0.5:9092"],
)
def test_a_well_formed_broker_list_still_passes(servers):
    cfg = {"source": {"bootstrapServers": servers}}
    refuse_unless_source_hosts_allowed(cfg, environ={HOST_ALLOWLIST_ENV: "ok"})
