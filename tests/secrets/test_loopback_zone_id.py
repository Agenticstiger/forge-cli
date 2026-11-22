# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A loopback address with an IPv6 zone id is not this machine.

:mod:`ipaddress` reads every character after ``%`` as the zone, so a zone can
carry a comma and a second host, which libpq connects to. The destination
policy exempts loopback hosts, so the zone would hide a remote one.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

import pytest

from fluid_build._net import is_loopback_host
from fluid_build.secrets.policy import AcquisitionSecretPolicyError, refuse_unless_allowed

ELSEWHERE = "elsewhere.example"


def _refusal(connection: Mapping[str, Any]) -> Optional[AcquisitionSecretPolicyError]:
    try:
        refuse_unless_allowed("env://PG", connection=connection, environ={})
    except AcquisitionSecretPolicyError as exc:
        return exc
    return None


@pytest.mark.unit
@pytest.mark.parametrize("host", [f"::1%x,{ELSEWHERE}", "::1%lo0", f"[::1%x,{ELSEWHERE}]"])
def test_a_loopback_address_with_a_zone_id_is_not_loopback(host):
    """libpq splits a host list on commas, so a zone id can carry a second host."""
    assert not is_loopback_host(host)
    assert _refusal({"host": host}) is not None


@pytest.mark.unit
def test_a_uri_authority_with_a_zone_id_is_not_loopback():
    assert _refusal({"uri": "postgresql://[::1%25lo0]/db"}) is not None


@pytest.mark.unit
@pytest.mark.parametrize("host", ["::1", "[::1]", "127.0.0.1", "127.8.9.10", "localhost"])
def test_loopback_addresses_without_a_zone_stay_loopback(host):
    assert is_loopback_host(host)
    assert _refusal({"host": host}) is None
