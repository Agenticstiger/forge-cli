# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The streaming unit tests never resolve real DNS."""

import socket

import pytest

from fluid_build._net import hostname_scope


def test_a_name_fails_at_once_and_is_recorded(offline_dns):
    assert hostname_scope("minio.lake.svc.cluster.local") == "unresolved"
    assert offline_dns == ["minio.lake.svc.cluster.local"]


def test_an_ip_literal_resolves_to_itself_without_a_lookup(offline_dns):
    assert hostname_scope("10.0.0.5") == "non_public"
    assert hostname_scope("8.8.8.8") == "public"
    assert offline_dns == []
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("example.invalid", 80)
