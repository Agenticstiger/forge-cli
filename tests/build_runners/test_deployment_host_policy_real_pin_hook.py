# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The deployment-host guard judges what the REAL pin hook pinned.

``tests/conftest.py`` replaces safe_http's pin hook with a no-op for the unit
suite, so ``test_deployment_host_policy.py`` hands the guard a stand-in
request. That cannot catch the one ordering the fix relies on: the pin hook
runs first and writes the IP the connection will use into ``request.url``,
and the guard reads it there. This file is on that fixture's real-guard list,
so the clients below run the real ``safe_httpx_client`` stack. A guard moved
ahead of the pin hook, or one that resolves the name again, lets the request
reach the transport and fails here.

DNS is faked and the transport records instead of sending, so nothing leaves
the machine.
"""

from __future__ import annotations

import socket
import sys
from typing import Any, Iterator, List, Tuple

import httpx
import pytest

from fluid_build.build_runners._deployment_host_policy import ALLOWLIST_ENV, DeploymentHostError

PUBLIC = "93.184.216.34"
PRIVATE = "10.1.2.3"

_REAL_GETADDRINFO = socket.getaddrinfo


def _answer(address: str) -> List[Tuple[Any, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]


def _getaddrinfo(host: str, *a: Any, **k: Any) -> List[Tuple[Any, ...]]:
    if host == "rebind.test":
        # The pin hook's lookup gets the attacker's public answer; every
        # lookup the policy makes of its own gets a private one.
        pinning = sys._getframe(1).f_code.co_name == "resolve_and_validate"
        return _answer(PUBLIC if pinning else PRIVATE)
    if host == "faß.de":  # what Python's resolver asks about: IDNA2003 fass.de
        return _answer(PRIVATE)
    if host == "xn--fa-hia.de":  # what httpx connects to: IDNA2008
        return _answer(PUBLIC)
    if host == "connect.internal":
        return _answer(PRIVATE)
    return _REAL_GETADDRINFO(host, *a, **k)


@pytest.fixture(autouse=True)
def sent(monkeypatch: pytest.MonkeyPatch) -> Iterator[List[str]]:
    """The URLs that reached the transport, which answers instead of sending."""
    reached: List[str] = []

    def _record(self: Any, request: httpx.Request) -> httpx.Response:
        reached.append(str(request.url))
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _record)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    yield reached


@pytest.mark.parametrize(
    "url",
    [
        "http://rebind.test:8083",  # DNS rebinding between the two lookups
        "http://faß.de:8083",  # IDNA2003 against httpx's IDNA2008
    ],
)
def test_a_name_that_looks_private_twice_is_refused_at_its_pinned_address(
    url: str, sent: List[str]
) -> None:
    from fluid_build.build_runners.kafka_connect.runner import KafkaConnectRestClient

    client = KafkaConnectRestClient(url)  # every check of the name says private
    try:
        with pytest.raises(DeploymentHostError) as excinfo:
            client.list_connectors()
    finally:
        client.close()
    assert sent == [], f"the config must not leave for {PUBLIC}, got {sent}"
    assert f"connecting to '{PUBLIC}'" in (excinfo.value.why or "")


def test_the_airbyte_client_is_guarded_the_same_way(sent: List[str]) -> None:
    from fluid_build.build_runners.airbyte.runner import AirbyteRestClient

    client = AirbyteRestClient("https://rebind.test", api_token="t")
    try:
        with pytest.raises(DeploymentHostError):
            client.list_sources("workspace")
    finally:
        client.close()
    assert sent == []


def test_a_private_orchestrator_is_reached_at_its_pinned_address(sent: List[str]) -> None:
    from fluid_build.build_runners.kafka_connect.runner import KafkaConnectRestClient

    client = KafkaConnectRestClient("http://connect.internal:8083")
    try:
        client.list_connectors()
    finally:
        client.close()
    assert sent == [f"http://{PRIVATE}:8083/connectors"]
