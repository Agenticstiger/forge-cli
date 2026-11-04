# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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

# CIDR ranges ``ipaddress.is_private`` does NOT flag but which are nonetheless
# non-routable / not safe to fetch from. Borrowed from requests-hardened
# (Saleor, BSD-3). Lives here, at tier-0, because BOTH the broad SSRF gate below
# and ``util.safe_http.is_public_address`` judge addresses and must agree: they
# did not before, and the acquisition deployment-host policy -- which allows
# internal hosts and questions public ones -- classified a Tailscale/CGNAT
# orchestrator as public and refused it with the wrong reason.
EXTRA_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "100.64.0.0/10",  # RFC 6598 — Carrier-Grade NAT (Tailscale, many ISPs)
        "192.0.0.0/24",  # RFC 6890 — IETF Protocol Assignments (CPython #113171 gap)
        "192.0.2.0/24",  # RFC 5737 — TEST-NET-1
        "192.88.99.0/24",  # RFC 7526 — 6to4 relay anycast (deprecated)
        "198.18.0.0/15",  # RFC 2544 — benchmarking
        "198.51.100.0/24",  # RFC 5737 — TEST-NET-2
        "203.0.113.0/24",  # RFC 5737 — TEST-NET-3
        "240.0.0.0/4",  # RFC 1112 — class E reserved
        "2001:20::/28",  # RFC 7343 — ORCHIDv2
        "2002::/16",  # RFC 3056 — 6to4
        "5f00::/16",  # RFC 9602 — IPv6 Segment Routing
        "64:ff9b::/96",  # RFC 6052 — Well-Known NAT64
        "64:ff9b:1::/48",  # RFC 8215 — Local-Use NAT64
    )
)

# Cloud metadata and credential endpoints that are NOT link-local, so the narrow
# gate below passed them as internal. They sit in ranges that are ordinary
# internal networks -- ULA for an IPv6 cluster, CGNAT for a Tailscale tailnet --
# so the ranges stay allowed and only these addresses are named, checked before
# any allowlist as rmcp-server-kit's ssrf.rs does.
CLOUD_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(addr)
    for addr in (
        "fd00:ec2::254",  # AWS EC2 instance metadata over IPv6 (Nitro, opt-in)
        "fd00:ec2::23",  # AWS EKS Pod Identity agent, IPv6
        "fd20:ce::254",  # GCP metadata server, IPv6-only VMs
        "fd00:c1::a9fe:a9fe",  # Oracle Cloud instance metadata, IPv6 (cloud-init)
        "100.100.100.200",  # Alibaba Cloud metadata, inside CGNAT
    )
)

# RFC 6052: a well-known NAT64 address carries its IPv4 address in the low 32 bits.
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")


def is_public_address(addr: str) -> bool:
    """True iff ``addr`` is a routable public IP (v4 or v6).

    Handles three traps ``ipaddress`` alone does not:
      * IPv4-mapped IPv6 (``::ffff:a.b.c.d``) — on Python <3.12 the v6 form does
        NOT recurse ``is_private`` / ``is_loopback`` into the embedded v4
        (gh-87105 / bpo-44904, fixed only in 3.12).
      * Zone-id-bearing v6 (``fe80::1%eth0``) — strip the scope.
      * Extended CIDR deny-list (:data:`EXTRA_BLOCKED_NETWORKS`).

    An address that does not parse is not public. ``util.safe_http`` re-exports
    this; it used to own it, and the two judgements must not drift.
    """
    if "%" in addr:
        addr = addr.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    for network in EXTRA_BLOCKED_NETWORKS:
        if ip.version == network.version and ip in network:
            return False
    return True


#: Every host resolves into exactly one of these.
#:
#: ``"public"``     — resolved, and EVERY address is publicly routable.
#: ``"non_public"`` — resolved, and EVERY address is loopback, private,
#:                    link-local, unspecified, reserved or on the extended
#:                    deny-list above.
#: ``"mixed"``      — resolved to BOTH. Not a curiosity: an attacker who names
#:                    the host answers with one private record beside their
#:                    public one, and a gate that stops at the first non-public
#:                    address calls the host internal while the connection is
#:                    pinned to the public one.
#: ``"unresolved"`` — DNS failed, or an address came back unparseable.
HOST_SCOPES = ("public", "non_public", "mixed", "unresolved")


def hostname_scope(hostname: str) -> str:
    """Which of :data:`HOST_SCOPES` ``hostname`` falls into.

    EVERY address in the answer is examined, and a mixed answer is reported as
    such. :func:`_hostname_is_private` cannot express that: it answers one
    question ("may I refuse this?") for which ``mixed`` and ``non_public`` are
    the same, while a gate with the opposite polarity -- one that ALLOWS
    internal hosts and asks the operator to name public ones -- must treat a
    mixed answer as public. Collapsing the two is a complete bypass of such a
    gate: one decoy AAAA record and the credential goes to the public A record
    ``safe_http`` then pins. Found by review of the deployment-host policy and
    reproduced before this existed.

    Kept here, beside :func:`is_public_address` and the range list it consults,
    rather than re-derived by a caller: this module's whole point is that the
    blocked set is written once.
    """
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return "unresolved"
    saw_public = False
    saw_non_public = False
    for entry in addresses:
        ip_str = entry[4][0]
        try:
            ipaddress.ip_address(ip_str.split("%", 1)[0])
        except ValueError:
            return "unresolved"
        if is_public_address(ip_str):
            saw_public = True
        else:
            saw_non_public = True
    if saw_public and saw_non_public:
        return "mixed"
    if saw_non_public:
        return "non_public"
    # No addresses at all lands here, as it did before: the old loop fell
    # through to "not private".
    return "public"


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

    Expressed in terms of :func:`hostname_scope` so the blocked range list is
    written once. ``unresolved``, ``non_public`` and ``mixed`` all answer True,
    which is what the old "any non-public address" loop did.

    One deliberate change: the extended deny-list is now consulted here too, so
    a CGNAT / 6to4 / NAT64 / TEST-NET host answers True where it used to answer
    False. That only ever refuses more, never allows more, and it matches what
    ``safe_http.resolve_and_validate`` already did for the same addresses --
    the two disagreed before.
    """
    return hostname_scope(hostname) != "public"


def _is_metadata_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Link-local, unspecified or a named metadata endpoint, seen through IPv6 wrappers.

    An IPv4-mapped address (``::ffff:169.254.169.254``) reaches the IPv4
    address from a dual-stack socket, and a NAT64 one (``64:ff9b::a9fe:a9fe``)
    reaches it through the gateway, so both are judged by the address they
    carry. ``is_link_local`` unwraps the mapped form only on interpreters with
    the CPython backport, so it is not relied on.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64_WELL_KNOWN:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_link_local or ip.is_unspecified or ip in CLOUD_METADATA_ADDRESSES


def _hostname_is_link_local(hostname: str) -> bool:
    """Return True when ``hostname`` resolves to a link-local or metadata address.

    The narrow companion to :func:`_hostname_is_private`, for the callers
    that *deliberately* accept an operator-configured internal endpoint —
    an on-prem Marquez, a DataHub GMS on the cluster network, a
    ``docker compose`` service name — but must still refuse the
    credential-bearing-POST-to-``169.254.169.254`` shape. Link-local
    (169.254.0.0/16, fe80::/10) is where AWS/GCP/Azure instance metadata
    lives and is never a legitimate service endpoint, so it stays blocked
    even when private ranges are allowed. So do the metadata endpoints that
    are not link-local (:data:`CLOUD_METADATA_ADDRESSES`) and either kind
    wrapped in an IPv4-mapped or NAT64 address. Unspecified (0.0.0.0, ::) is
    refused for the same reason it is in the broad gate: on many stacks it
    routes to loopback.

    Unlike the broad gate this returns ``False`` on DNS failure. An
    unresolvable host cannot be reached at all, so the connection attempt
    fails on its own and the caller surfaces an accurate transport error
    instead of a misleading "address is private" refusal — which is what
    made a ``docker compose`` hostname like ``marquez`` look like a
    security block rather than "that container is not running".
    """
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for entry in addresses:
        ip_str = entry[4][0]
        try:
            # Without the zone, so fd00:ec2::254%eth0 is still the metadata address.
            ip = ipaddress.ip_address(str(ip_str).split("%", 1)[0])
        except ValueError:
            return True
        if _is_metadata_address(ip):
            return True
    return False
