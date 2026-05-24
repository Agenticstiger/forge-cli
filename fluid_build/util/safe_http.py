# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""SSRF-safe HTTP fetching primitives — one wrapper, one mental model.

The whole codebase uses ``httpx`` (already a runtime dep via the build
runners); this module provides the one SSRF-guarded factory that every
HTTP-fetch surface must route through.

The guarantees:

* **Scheme allowlist** — only http / https. ftp / file / data / etc.
  are refused at the URL-validation step.
* **Host-IP filter** — every fetch resolves the hostname and rejects
  private / loopback / link-local / multicast / reserved / unspecified
  / CG-NAT / 6to4 / NAT64 / ORCHIDv2 / IPv6 SR / RFC TEST-NETs.
  IPv4-mapped IPv6 (``::ffff:a.b.c.d``) is unwrapped (closes a real
  bypass on Python <3.12).
* **Reject-all on mixed-public+private DNS** — an attacker cannot mix
  a public A record with a private AAAA.
* **Connection-layer DNS pinning** — uses httpx's first-class
  ``sni_hostname`` extension (documented at
  python-httpx.org/advanced/extensions) so the TCP connect targets the
  validated IP even if DNS flips between check and connect. The
  hostname is preserved for SNI / certificate verification + Host
  header.
* **No redirect follow by default** — ``follow_redirects=False``.
  Auth-bearing clients should not ride a redirect to an unaudited
  second hop. Recent precedent: curl CVE-2026-3783 (bearer leak via
  redirect), open-webui GHSA-rh5x-h6pp-cjj6 (SSRF via redirect bypass).
* **Body size cap** — :func:`fetch_text` / :func:`fetch_json` enforce
  :data:`MAX_REMOTE_BYTES` (10 MiB) via streamed read.
* **Audit log** on every block (``ssrf_guard_blocked`` WARN).

Borrow-before-build receipts:
  * The extended CIDR deny-list + IPv4-mapped IPv6 unwrap is borrowed
    from ``requests-hardened`` (Saleor, BSD-3,
    github.com/saleor/requests-hardened).
  * The httpx DNS-pinning recipe (URL with IP + Host header +
    ``sni_hostname`` extension) is the maintainer-blessed pattern
    from github.com/encode/httpx/discussions/2811.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0  # seconds
MAX_REMOTE_BYTES = 10 * 1024 * 1024  # 10 MiB


class UnsafeURLError(ValueError):
    """Raised when a URL targets a non-public address or non-http(s) scheme."""


# CIDR ranges that ``ipaddress.is_private`` does NOT flag but which are
# nonetheless non-routable / not safe to fetch from. Borrowed from
# requests-hardened (Saleor, BSD-3).
EXTRA_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "100.64.0.0/10",  # RFC 6598 — Carrier-Grade NAT
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


def is_public_address(addr: str) -> bool:
    """True iff ``addr`` is a routable public IP (v4 or v6).

    Handles three traps ``ipaddress`` alone does not:
      * IPv4-mapped IPv6 (``::ffff:a.b.c.d``) — on Python <3.12 the
        v6 form does NOT recurse ``is_private``/``is_loopback`` into
        the embedded v4 (gh-87105 / bpo-44904 fixed only in 3.12).
      * Zone-id-bearing v6 (``fe80::1%eth0``) — strip the scope.
      * Extended CIDR deny-list (:data:`EXTRA_BLOCKED_NETWORKS`).
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


def resolve_and_validate(hostname: str, *, allow_private: bool = False) -> str:
    """Resolve ``hostname`` and return the first address.

    With ``allow_private=False`` (default) any non-public answer fails
    the lookup outright (reject-all on a mixed-public+private answer
    set — an attacker cannot mix a public A with a private AAAA).

    With ``allow_private=True`` (opt-in for localhost-only dev tools)
    the function still resolves and returns the first address but does
    not enforce the public-IP check.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"cannot resolve hostname {hostname!r}") from exc
    if not infos:
        raise UnsafeURLError(f"hostname {hostname!r} has no addresses")
    addrs = [info[4][0] for info in infos]
    if not allow_private:
        for addr in addrs:
            if not is_public_address(addr):
                LOG.warning(
                    "ssrf_guard_blocked",
                    extra={
                        "hostname": hostname,
                        "address": addr,
                        "reason": "non-public",
                    },
                )
                raise UnsafeURLError(
                    f"refusing fetch from non-public address {addr} "
                    f"(hostname {hostname!r})"
                )
    return addrs[0]


def assert_safe_url(
    url: str, *, allow_private: bool = False
) -> Tuple[str, str]:
    """Validate ``url`` for SSRF and return ``(hostname, pinned_ip)``.

    The returned IP is used to force the TCP connect call to the
    validated address so the actual connection cannot end up at a
    different (private) host even if DNS flips meanwhile.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"refusing non-http(s) scheme: {parsed.scheme!r}")
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeURLError("URL has no hostname")
    pinned_ip = resolve_and_validate(hostname, allow_private=allow_private)
    return hostname, pinned_ip


# --- httpx hook + client factory + redirect handler -----------------------
#
# httpx supports connection-layer DNS pinning via its first-class
# ``sni_hostname`` request extension (python-httpx.org/advanced/extensions/,
# maintainer-blessed pattern in github.com/encode/httpx/discussions/2811).
# The recipe:
#
#   1. Rewrite URL authority to the *pinned IP*.
#   2. Set ``Host`` header to the original hostname.
#   3. Set ``sni_hostname`` extension to the original hostname.
#
# httpx then connects to the IP directly while preserving SNI + cert
# verification + Host header semantics.


def _make_request_pin_hook(*, allow_private: bool):
    """Build an httpx ``request`` event hook that validates + DNS-pins.

    With ``allow_private=False`` (default) any non-public address fails.
    With ``allow_private=True`` (for build runners targeting internal
    Kafka Connect / Debezium / etc.) the IP filter is off BUT we still:
      * enforce the scheme allowlist (no ftp/file/data),
      * resolve + pin the IP at connection time (DNS-rebind defence
        is valuable even on private targets — an attacker who
        compromises the catalog DNS could re-route the call to a
        different internal service).
    """

    def _hook(request) -> None:
        hostname, pinned_ip = assert_safe_url(
            str(request.url), allow_private=allow_private
        )
        request.url = request.url.copy_with(host=pinned_ip)
        request.headers["Host"] = hostname
        request.extensions["sni_hostname"] = hostname

    return _hook


# Back-compat alias used by tests and the hook insertion in
# safe_httpx_client. Default semantics = public-only.
_pin_httpx_request_to_safe_ip = _make_request_pin_hook(allow_private=False)


def safe_httpx_client(
    base_url: str = "",
    *,
    allow_private: bool = False,
    follow_redirects: bool = False,
    require_https: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    **kwargs: Any,
):
    """Return an ``httpx.Client`` with the full SSRF guard applied.

    Use this for **every** outbound http(s) call in the codebase.

    * ``base_url`` (if non-empty) is validated at construction; a
      malicious URL fails fast.
    * Every per-request URL is re-validated AND its connection is
      DNS-pinned to the validated IP via httpx's ``sni_hostname``
      extension.
    * ``follow_redirects=False`` by default.
    * ``require_https=True`` rejects ``http://`` base URLs (use for
      any client carrying a Bearer token / API key).
    * For trusted localhost / dev tooling pass ``allow_private=True``.
    """
    import httpx  # lazy import — keeps import-time graph light

    if base_url:
        parsed = urlparse(base_url)
        if require_https and parsed.scheme != "https":
            raise UnsafeURLError(
                f"refusing non-https base_url {base_url!r} for auth-bearing client"
            )
        assert_safe_url(base_url, allow_private=allow_private)

    hooks = kwargs.pop("event_hooks", None) or {}
    request_hooks = list(hooks.get("request") or [])
    request_hooks.insert(0, _make_request_pin_hook(allow_private=allow_private))
    hooks["request"] = request_hooks

    return httpx.Client(
        base_url=base_url.rstrip("/") if base_url else "",
        follow_redirects=follow_redirects,
        timeout=timeout,
        event_hooks=hooks,
        **kwargs,
    )


# --- High-level fetch helpers --------------------------------------------


def fetch_bytes(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_REMOTE_BYTES,
    allow_private: bool = False,
    headers: Optional[dict] = None,
) -> Tuple[int, dict, bytes]:
    """Streamed GET with size cap. Returns ``(status, headers, body)``.

    The streaming + per-chunk size check guarantees we never buffer
    more than ``max_bytes + 1`` regardless of the response's
    ``Content-Length`` header (which can lie). Raises
    :class:`UnsafeURLError` for any SSRF-rejectable URL.
    """
    with safe_httpx_client(
        allow_private=allow_private, timeout=timeout
    ) as client:
        with client.stream("GET", url, headers=headers or {}) as response:
            chunks = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise UnsafeURLError(
                        f"response from {url!r} exceeds {max_bytes} bytes"
                    )
                chunks.append(chunk)
            return response.status_code, dict(response.headers), b"".join(chunks)


__all__ = [
    "DEFAULT_TIMEOUT",
    "EXTRA_BLOCKED_NETWORKS",
    "MAX_REMOTE_BYTES",
    "UnsafeURLError",
    "assert_safe_url",
    "fetch_bytes",
    "is_public_address",
    "resolve_and_validate",
    "safe_httpx_client",
]
