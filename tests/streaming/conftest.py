# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

import ipaddress
import socket
from pathlib import Path

import pytest
import yaml

from fluid_build.streaming.policy import (
    CONTEXT_ALLOWLIST_ENV,
    HOST_ALLOWLIST_ENV,
    IMAGE_ALLOWLIST_ENV,
    NAMESPACE_ALLOWLIST_ENV,
    SECRET_ALLOWLIST_ENV,
    SERVICE_ACCOUNT_ALLOWLIST_ENV,
)

#: What an operator lists to deploy examples/flink-iceberg (deny unless listed).
EXAMPLE_ALLOWLISTS = {
    CONTEXT_ALLOWLIST_ENV: "development",
    NAMESPACE_ALLOWLIST_ENV: "data-products",
    IMAGE_ALLOWLIST_ENV: "forge-flink",
    SERVICE_ACCOUNT_ALLOWLIST_ENV: "data-products/flink",
    SECRET_ALLOWLIST_ENV: "data-products/flink-storage",  # pragma: allowlist secret - a name
}


@pytest.fixture
def contract():
    root = Path(__file__).resolve().parents[2]
    return yaml.safe_load((root / "examples/flink-iceberg/contract.fluid.yaml").read_text())


@pytest.fixture(autouse=True)
def operator_allows_the_example(monkeypatch):
    """The example's deployment targets and catalog, listed as its operator would."""
    for name, value in EXAMPLE_ALLOWLISTS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "iceberg-rest,kafka")  # names the job resolves


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    """No unit test resolves real DNS: a name fails at once, an IP literal is itself.

    A lookup of a cluster-internal name (``minio.lake.svc.cluster.local``) takes
    seconds against a real resolver, and its answer would depend on the machine.
    Returns the names that were looked up.
    """
    looked_up: list[str] = []
    real = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        text = host.decode() if isinstance(host, bytes) else str(host)
        try:
            address = ipaddress.ip_address(text.split("%", 1)[0])
        except ValueError:
            looked_up.append(text)
            raise socket.gaierror(socket.EAI_NONAME, "name resolution is stubbed in unit tests")
        return real(str(address), port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return looked_up
