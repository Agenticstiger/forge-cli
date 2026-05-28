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

"""Tier-0 networking primitives.

Stdlib-only leaf module — must not import from any other
``fluid_build.*`` package. Lives at the bottom of the import graph so
both ``observability`` and ``build_runners`` (and ``cli``, and
``forge``) can depend on it without inducing a cycle. This invariant
is enforced by the ``fluid_build._net is tier-0`` contract in
``[tool.importlinter]`` (``pyproject.toml``).

Only contains the canonical SSRF post-DNS-resolution gate today; future
cross-cutting net helpers may be added here as long as they remain
stdlib-only.
"""

from __future__ import annotations

import ipaddress
import socket


def _hostname_is_private(hostname: str) -> bool:
    """Return True when ``hostname`` resolves to a non-public IP.

    Considers loopback, private, link-local, and unspecified IPv4/IPv6
    ranges (this catches AWS/GCP metadata at 169.254.169.254 and
    on-host services). DNS resolution errors fall back to refusing the
    request (better to fail-closed than fan-out to unknowns).

    .. note::

        This is the canonical SSRF post-DNS-resolution gate for the
        whole codebase. It is deliberately re-used by the federation
        digest fetchers (:mod:`fluid_build.forge.federation`), the
        OpenLineage HTTP emitter (:mod:`fluid_build.build_runners._lineage`),
        the DLQ webhook alerter (:mod:`fluid_build.build_runners._alerter`),
        and the Command Center client/reporter
        (:mod:`fluid_build.cli._command_center`,
        :mod:`fluid_build.observability.reporter`) rather than each
        call site re-deriving its own private-range list. When
        extending the blocked set, extend it *here* so every consumer
        benefits.

        This module lives at tier-0 of the import graph (no
        ``fluid_build.*`` upstreams) so that ``observability`` can
        depend on it without re-introducing the
        ``observability → build_runners → cli → observability``
        cycle that previously broke ``cli/__init__.py`` import-time.
    """
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return True
    for entry in addresses:
        ip_str = entry[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return True
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_unspecified
            or ip.is_reserved
        ):
            return True
    return False
