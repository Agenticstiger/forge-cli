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

"""A public orchestrator may not receive a product's credential unnamed.

On the kafka-connect path, and on the DEFAULT ``bring-your-own`` / ``managed``
modes of debezium and airbyte, forge-cli never connects to the source database:
it resolves the contract's ``secretRef``, puts the plaintext into the connector
config and POSTs the whole config to
``properties.<engine>.deployment.server_url`` -- a URL the contract names.

The policy: a PUBLIC host must be named in
``FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST``; loopback and private hosts need
no entry; link-local is refused outright; an operator-supplied URL is not
second-guessed.

**DNS is faked throughout.** An earlier version of this file resolved
``example.com`` for its public cases and skipped when that failed, which meant
six of its eight DNS-dependent assertions could pass without testing anything,
and the comment claiming otherwise was wrong. Every host here is answered by
:func:`_fake_getaddrinfo`, so the suite is deterministic and offline, and the
mixed-answer case -- which no earlier test covered and which was a complete
bypass -- can be stated exactly.

The three things this file exists to pin, each of which was broken when first
written and is mutation-checked:

1. **A mixed DNS answer is not "internal".** One private record beside a public
   one made ``hostname_scope`` say ``non_public``, the policy allow the host
   unlisted, and ``safe_http`` pin the public address -- the exact leak the
   policy exists to stop, with no operator error.
2. **The host is judged before the secret is resolved.** The client constructor
   is the chokepoint, but the runners build the connector config first, and
   that resolves the ``secretRef``. The gate therefore also runs where
   ``server_url`` is read, and the test drives the real runner with the spy
   patched where the runner looks the name up.
3. **The per-request guard really fires.** Asserting ``len(event_hooks) >= 2``
   passed for a no-op guard; this fires a request and asserts nothing is sent.
"""

from __future__ import annotations

import json
import pathlib
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from fluid_build._net import _hostname_is_private, hostname_scope, is_public_address
from fluid_build.build_runners._deployment_host_policy import (
    ALLOWLIST_ENV,
    DeploymentHostError,
    host_refusal,
    refuse_unless_host_allowed,
    request_guard,
)

#: Synthetic hosts and the addresses DNS will answer for them. Names, not live
#: domains, so no assertion here depends on the network or on who owns what.
FAKE_DNS: Dict[str, List[str]] = {
    "orchestrator.public.test": ["93.184.216.34"],
    "orchestrator.private.test": ["10.1.2.3"],
    "orchestrator.loopback.test": ["127.0.0.1"],
    "orchestrator.cgnat.test": ["100.100.100.100"],  # Tailscale / RFC 6598
    "orchestrator.metadata.test": ["169.254.169.254"],
    # The bypass: a decoy private record beside the attacker's public one.
    "orchestrator.mixed.test": ["fd00::1", "93.184.216.34"],
    "orchestrator.v6public.test": ["2606:2800:220:1:248:1893:25c8:1946"],
    "orchestrator.mappedprivate.test": ["::ffff:10.1.2.3"],
}


_REAL_GETADDRINFO = socket.getaddrinfo


def _fake_getaddrinfo(host: str, *a: Any, **k: Any) -> List[Tuple[Any, ...]]:
    """Answer from :data:`FAKE_DNS`; a NAME outside it does not resolve.

    A literal address is passed to the real resolver, which does not touch the
    network for one, so ``127.0.0.1`` and ``169.254.169.254`` behave normally
    and the loopback listener test still works. Only names are synthetic, which
    is the part that would otherwise depend on DNS.
    """
    import ipaddress as _ip

    try:
        _ip.ip_address(str(host).strip("[]").split("%", 1)[0])
    except ValueError:
        pass
    else:
        return _REAL_GETADDRINFO(host, *a, **k)
    if host not in FAKE_DNS:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    out: List[Tuple[Any, ...]] = []
    for addr in FAKE_DNS[host]:
        if ":" in addr:
            out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, 0, 0, 0)))
        else:
            out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)))
    return out


@pytest.fixture(autouse=True)
def _dns(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test in this file resolves through :data:`FAKE_DNS`.

    Patched on ``socket`` itself, which is what ``_net`` and ``safe_http`` both
    call, so the policy and the transport layer agree on what a host is.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    yield


# ── the classifier the policy needed ────────────────────────────────────


@pytest.mark.parametrize(
    "host, scope",
    [
        ("orchestrator.public.test", "public"),
        ("orchestrator.v6public.test", "public"),
        ("orchestrator.private.test", "non_public"),
        ("orchestrator.loopback.test", "non_public"),
        ("orchestrator.metadata.test", "non_public"),
        ("orchestrator.cgnat.test", "non_public"),
        ("orchestrator.mappedprivate.test", "non_public"),
        ("orchestrator.mixed.test", "mixed"),
        ("orchestrator.absent.test", "unresolved"),
    ],
)
def test_hostname_scope_examines_every_address(host: str, scope: str) -> None:
    assert hostname_scope(host) == scope


@pytest.mark.parametrize(
    "host, private",
    [
        ("orchestrator.public.test", False),
        ("orchestrator.v6public.test", False),
        ("orchestrator.private.test", True),
        ("orchestrator.loopback.test", True),
        ("orchestrator.metadata.test", True),
        ("orchestrator.mixed.test", True),
        ("orchestrator.absent.test", True),
        ("orchestrator.cgnat.test", True),
    ],
)
def test_the_broad_gate_keeps_its_fail_closed_direction(host: str, private: bool) -> None:
    """``_hostname_is_private`` has five callers and must not loosen.

    Both directions are asserted, not just the True ones: a function that
    always returned True would satisfy a one-sided test and silently break
    every SSRF gate's ability to allow a legitimate public fetch.

    ``mixed`` and ``unresolved`` answer True here -- that is this gate's safe
    direction, and the opposite of the deployment-host policy's, which is why
    the classifier keeps the states apart. CGNAT answers True where it once
    answered False: the extended deny-list is now consulted here too, which
    only ever refuses more and matches what ``safe_http`` already did.
    """
    assert _hostname_is_private(host) is private


def test_the_address_predicate_is_shared_not_copied() -> None:
    """``safe_http`` re-exports the tier-0 definitions rather than owning them.

    They disagreed before: bare ``ipaddress`` flags here, the extended
    deny-list there, so one CGNAT address was public to one and private to the
    other.
    """
    from fluid_build.util import safe_http

    assert safe_http.is_public_address is is_public_address
    from fluid_build import _net

    assert safe_http.EXTRA_BLOCKED_NETWORKS is _net.EXTRA_BLOCKED_NETWORKS


# ── the policy ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url, listed, allowed, because",
    [
        ("http://orchestrator.loopback.test:8083", "", True, "loopback needs no entry"),
        ("http://orchestrator.private.test:8083", "", True, "the ordinary cluster install"),
        ("http://orchestrator.cgnat.test:8083", "", True, "Tailscale is an internal network"),
        ("https://orchestrator.public.test", "", False, "a public host must be named"),
        ("https://orchestrator.public.test", "orchestrator.public.test", True, "named exactly"),
        ("https://orchestrator.public.test", "public.test", True, "dotted suffix, as siblings do"),
        ("https://orchestrator.public.test", "other.test", False, "a different entry is not this"),
        (
            "https://orchestrator.public.test",
            "ublic.test",
            False,
            "a bare substring is not a suffix",
        ),
        (
            "https://orchestrator.public.test",
            "orchestrator.public.test:443",
            False,
            "ports never match",
        ),
        ("https://ORCHESTRATOR.PUBLIC.TEST", "orchestrator.public.test", True, "case-insensitive"),
        ("https://orchestrator.public.test.", "orchestrator.public.test", True, "trailing dot"),
        ("https://orchestrator.mixed.test", "", False, "a decoy private record is still public"),
        ("https://orchestrator.mixed.test", "mixed.test", True, "named, so the operator chose it"),
        ("http://orchestrator.metadata.test", "", False, "instance metadata"),
        ("http://orchestrator.metadata.test", "metadata.test", False, "metadata, even named"),
        ("http://169.254.169.254", "169.254.169.254", False, "by address, even named"),
        ("http://0.0.0.0", "", False, "unspecified routes to loopback on many stacks"),
        ("file:///etc/passwd", "", False, "not an http(s) orchestrator"),
        ("", "", False, "nothing configured"),
        ("https://", "", False, "no host"),
    ],
)
def test_the_policy_matrix(url: str, listed: str, allowed: bool, because: str) -> None:
    refusal = host_refusal(url, engine="kafka-connect", environ={ALLOWLIST_ENV: listed})
    assert (refusal is None) is allowed, f"{because}: {url!r} listed={listed!r} -> {refusal!r}"


def test_a_mixed_answer_is_refused_for_what_it_is() -> None:
    """The message has to name the real reason, or the reader cannot act.

    "is public" would be misleading for a host that does answer with a private
    address; the operator needs to know a decoy record is what they are seeing.
    """
    refusal = host_refusal("https://orchestrator.mixed.test", engine="airbyte", environ={})
    assert refusal is not None
    assert "both public and non-public" in refusal, refusal


def test_an_unresolvable_host_is_not_this_gate_s_refusal() -> None:
    """It is refused by the transport layer instead, with an accurate message.

    Refusing it here turned "that host is not running" into a security error
    for ``kafka-connect.internal`` on a developer's machine.
    """
    assert host_refusal("http://orchestrator.absent.test:8083", engine="kafka-connect") is None

    # The transport layer is what refuses it -- ``resolve_and_validate`` raises
    # ``UnsafeURLError("cannot resolve hostname")`` before a request leaves. Not
    # asserted here: ``tests/conftest.py::_disable_ssrf_guard_for_unit_tests``
    # no-ops ``assert_safe_url`` for every file except the two that exercise the
    # guard for real, so an assertion here would pass or fail on the stub rather
    # than on the guard. ``tests/util/test_safe_http.py`` owns that case.


def test_an_operator_chosen_url_is_not_second_guessed() -> None:
    """A CLI flag or env var is the operator's choice, not a contract's."""
    assert host_refusal("https://orchestrator.public.test", engine="airbyte") is not None
    assert (
        host_refusal("https://orchestrator.public.test", engine="airbyte", operator_supplied=True)
        is None
    )


@pytest.mark.parametrize(
    "url",
    ["http://orchestrator.metadata.test", "http://0.0.0.0", "file:///etc/passwd"],
)
def test_what_an_operator_can_never_ask_for(url: str) -> None:
    assert host_refusal(url, engine="airbyte", operator_supplied=True) is not None


def test_the_refusal_names_the_variable_the_host_and_the_port_trap() -> None:
    with pytest.raises(DeploymentHostError) as excinfo:
        refuse_unless_host_allowed("https://orchestrator.public.test", engine="debezium")
    error = excinfo.value
    rendered = " ".join([str(error), error.why or "", error.fix or ""])
    assert ALLOWLIST_ENV in rendered
    assert "orchestrator.public.test" in rendered
    assert "debezium" in rendered
    assert "port" in rendered
    assert error.code == "DeploymentHostError"


def test_the_operator_facing_refusal_does_not_blame_a_contract() -> None:
    with pytest.raises(DeploymentHostError) as excinfo:
        refuse_unless_host_allowed(
            "http://orchestrator.metadata.test", engine="airbyte", operator_supplied=True
        )
    error = excinfo.value
    rendered = " ".join([str(error), error.why or "", error.fix or ""])
    assert "contract" not in rendered, rendered
    assert ALLOWLIST_ENV not in rendered, rendered
    assert "metadata" in rendered, rendered


def test_the_variable_is_operator_only() -> None:
    """A project `.env` beside the contract must not grant the allowlist."""
    from fluid_build.credentials.protected_env import is_protected_env_key

    assert is_protected_env_key(ALLOWLIST_ENV)


def test_the_allowlist_is_read_from_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert host_refusal("https://orchestrator.public.test", engine="airbyte") is not None
    monkeypatch.setenv(ALLOWLIST_ENV, "orchestrator.public.test")
    assert host_refusal("https://orchestrator.public.test", engine="airbyte") is None


# ── the clients ──────────────────────────────────────────────────────────


def test_both_rest_clients_refuse_a_public_unnamed_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fluid_build.build_runners.airbyte.runner import AirbyteRestClient
    from fluid_build.build_runners.kafka_connect.runner import KafkaConnectRestClient

    with pytest.raises(DeploymentHostError):
        KafkaConnectRestClient("https://orchestrator.public.test")
    with pytest.raises(DeploymentHostError):
        AirbyteRestClient("https://orchestrator.public.test", api_token="t")

    monkeypatch.setenv(ALLOWLIST_ENV, "orchestrator.public.test")
    KafkaConnectRestClient("https://orchestrator.public.test").close()
    AirbyteRestClient("https://orchestrator.public.test", api_token="t").close()


def test_the_engine_named_in_the_refusal_is_the_caller_s() -> None:
    """debezium shares the kafka-connect client; the message must not say so."""
    from fluid_build.build_runners.kafka_connect.runner import KafkaConnectRestClient

    with pytest.raises(DeploymentHostError) as excinfo:
        KafkaConnectRestClient("https://orchestrator.public.test", engine="debezium")
    assert "debezium" in (excinfo.value.why or "")


# ── the per-request guard, fired for real ────────────────────────────────


def test_the_guard_refuses_a_host_that_only_resolves_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construction allows an unresolved host; the request must not go out.

    The previous version of this test asserted ``len(event_hooks) >= 2``, which
    a no-op guard satisfies. This builds the client while the name does not
    resolve, makes it resolve to a public address, fires a real request, and
    asserts both the refusal and that the listener received nothing.
    """
    from fluid_build.build_runners.kafka_connect.runner import KafkaConnectRestClient

    received: List[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib spelling
            received.append(self.path)
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b"[]")

        def log_message(self, *_a: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Unresolved at construction: allowed, and no request has gone out.
        client = KafkaConnectRestClient("http://orchestrator.late.test:%d" % port)
        try:
            # Now it resolves -- to the listener, which is loopback, so prove
            # the guard judges the NAME's scope rather than the socket: make
            # the name answer with a public address while the connection still
            # lands on the listener.
            FAKE_DNS["orchestrator.late.test"] = ["93.184.216.34"]
            with pytest.raises(DeploymentHostError):
                client.list_connectors()
            assert received == [], f"the request must not reach the orchestrator, got {received}"
        finally:
            FAKE_DNS.pop("orchestrator.late.test", None)
            client.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_the_guard_judges_the_original_host_not_the_pinned_ip() -> None:
    """The SSRF hook rewrites the URL to an IP before this one runs."""

    class _Explodes:
        @property
        def url(self) -> str:
            raise AssertionError("the guard must not read the rewritten request URL")

    import os

    os.environ[ALLOWLIST_ENV] = "orchestrator.public.test"
    try:
        request_guard("https://orchestrator.public.test", engine="airbyte")(_Explodes())
    finally:
        del os.environ[ALLOWLIST_ENV]


@pytest.mark.parametrize(
    "module",
    [
        "fluid_build.build_runners.kafka_connect.runner",
        "fluid_build.build_runners.airbyte.runner",
    ],
)
def test_every_client_call_uses_a_relative_path(module: str) -> None:
    """An absolute URL would evade a guard that closes over the base URL."""
    import importlib
    import re

    source = pathlib.Path(importlib.import_module(module).__file__).read_text(encoding="utf-8")
    calls = re.findall(r"self\._client\.(?:get|post|put|delete|request)\(\s*(.)", source)
    assert calls, f"no client calls found in {module}; has the client moved?"
    assert not re.search(
        r"self\._client\.(?:get|post|put|delete|request)\(\s*f?\"https?://", source
    ), f"{module}: a client call passes an absolute URL, which the guard cannot police"


# ── the ordering guarantee, driven through the real runner ───────────────


def test_a_refused_deployment_never_resolves_the_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The claim the docs make, tested where it can actually fail.

    The client constructor is the chokepoint, but the runner builds the
    connector config first and THAT resolves the ``secretRef`` -- so the gate
    also runs where ``server_url`` is read. Two things the earlier version of
    this test got wrong: it constructed the client directly instead of driving
    the runner, and it patched ``_acquisition_common.resolve_connection_secrets``
    while the runner binds that name in its own module namespace, so the patch
    could never have been reached.
    """
    from fluid_build.build_runners.kafka_connect import runner as kc
    from tests.build_runners.test_kafka_connect_full_matrix import _base_contract

    def _never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a secret was resolved for a refused orchestrator")

    monkeypatch.setattr(kc, "resolve_connection_secrets", _never)

    contract = _base_contract(
        source={
            "kind": "postgres",
            "connection": {
                "host": "db.internal",
                "port": 5432,
                "database": "x",
                "user": "u",
                "secretRef": "env://DB_SECRET",  # pragma: allowlist secret
            },
            "mode": "full_refresh",
            "streams": ["public.orders"],
        },
        kc_props={"deployment": {"server_url": "https://orchestrator.public.test"}},
    )

    with pytest.raises(DeploymentHostError):
        kc.execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)


def test_a_loopback_orchestrator_still_receives_the_connector_config() -> None:
    """The policy must not break the local path it exempts.

    Also the honest demonstration of the defect: the POSTed body carries the
    resolved password. It goes to a listener on this machine.
    """
    from fluid_build.build_runners.kafka_connect.runner import KafkaConnectRestClient

    received: List[Tuple[str, str]] = []

    class _Captor(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib spelling
            n = int(self.headers.get("content-length") or 0)
            received.append((self.path, self.rfile.read(n).decode() if n else ""))
            self.send_response(201)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"name": "probe"}')

        def log_message(self, *_a: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Captor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = KafkaConnectRestClient(f"http://127.0.0.1:{server.server_address[1]}")
        try:
            client.create_connector(
                "probe",
                {
                    "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
                    "database.hostname": "db.internal",
                    "database.password": CONTRACT_PASSWORD,
                },
            )
        finally:
            client.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert received, "the loopback deployment must still be reachable"
    path, body = received[0]
    assert path == "/connectors"
    assert json.loads(body)["config"]["database.password"] == CONTRACT_PASSWORD


#: Stands in for a password the runner resolved from the contract.
CONTRACT_PASSWORD = "resolved-from-the-contract"  # pragma: allowlist secret
