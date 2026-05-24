# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Unit tests for fluid_build.util.safe_http.

Pins:
  * The shared SSRF primitives behave identically when called via
    the safe_http module path (versus the resolver's back-compat
    re-exports — covered in test_odps_resolver.py).
  * The httpx wrapper rewrites every outgoing request to the pinned
    IP, sets Host header + sni_hostname extension, and blocks unsafe
    URLs at request time.
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

httpx = pytest.importorskip("httpx")

from fluid_build.util.safe_http import (
    UnsafeURLError,
    _pin_httpx_request_to_safe_ip,
    assert_safe_url,
    is_public_address,
    safe_httpx_client,
)


class TestPublicAddressMatrix:
    """The full deny matrix — IPv4 / IPv6 / IPv4-mapped / extended CIDRs."""

    @pytest.mark.parametrize(
        "addr",
        [
            "169.254.169.254",  # IMDS
            "127.0.0.1",
            "::1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.1.1",  # link-local
            "100.64.0.1",  # CGNAT
            "192.0.0.1",  # IETF Protocol Assignments
            "192.0.2.1",  # TEST-NET-1
            "192.88.99.1",  # 6to4 relay
            "198.18.0.1",  # benchmarking
            "198.51.100.1",  # TEST-NET-2
            "203.0.113.1",  # TEST-NET-3
            "240.0.0.1",  # class E
            "255.255.255.255",  # broadcast
            "0.0.0.0",  # unspecified
            "::ffff:10.0.0.1",  # IPv4-mapped IPv6 → private
            "::ffff:127.0.0.1",  # IPv4-mapped IPv6 → loopback
            "::ffff:169.254.169.254",  # IPv4-mapped IPv6 → IMDS
            "2001:20::1",  # ORCHIDv2
            "2002::1",  # 6to4
            "5f00::1",  # IPv6 SR
            "64:ff9b::1",  # NAT64
            "fe80::1",  # link-local v6
            "fc00::1",  # ULA
            "ff00::1",  # multicast v6
        ],
    )
    def test_rejected(self, addr: str) -> None:
        assert not is_public_address(addr)

    @pytest.mark.parametrize(
        "addr",
        [
            "8.8.8.8",
            "1.1.1.1",
            "93.184.216.34",
            "2606:4700:4700::1111",  # Cloudflare DNS over IPv6
        ],
    )
    def test_accepted(self, addr: str) -> None:
        assert is_public_address(addr)


class TestAssertSafeURL:
    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/x",
            "file:///etc/passwd",
            "gopher://example.com/",
            "jar:https://example.com/x!/",
            "data:text/plain,foo",
            "javascript:alert(1)",
        ],
    )
    def test_rejects_non_http_scheme(self, url: str) -> None:
        with pytest.raises(UnsafeURLError):
            assert_safe_url(url)

    def test_userinfo_confusion_not_a_bypass(self) -> None:
        """``https://safe.com@169.254.169.254/`` parses host as the IMDS
        address — urlparse extracts the authority correctly."""
        def _fake(_host, _port, *_a, **_kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))]

        with patch("fluid_build.util.safe_http.socket.getaddrinfo", side_effect=_fake):
            with pytest.raises(UnsafeURLError):
                assert_safe_url("https://safe.com@169.254.169.254/x")

    def test_allow_private_lets_localhost_through(self) -> None:
        """``allow_private=True`` opt-out for trusted localhost dev tools."""
        def _fake(_host, _port, *_a, **_kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]

        with patch("fluid_build.util.safe_http.socket.getaddrinfo", side_effect=_fake):
            # Default rejects.
            with pytest.raises(UnsafeURLError):
                assert_safe_url("http://localhost:8080/")
            # Opt-in allows.
            host, ip = assert_safe_url("http://localhost:8080/", allow_private=True)
            assert host == "localhost"
            assert ip == "127.0.0.1"


class TestHttpxPinning:
    """The request event-hook must rewrite URL to pinned IP, set Host
    header, and set sni_hostname extension."""

    def test_hook_rewrites_url_and_sets_host(self) -> None:
        pinned_ip = "93.184.216.34"

        # Build a real httpx Request, then run our hook against it.
        request = httpx.Request("GET", "https://example.com/foo?q=1")

        def _fake(_host, _port, *_a, **_kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (pinned_ip, 0))]

        with patch("fluid_build.util.safe_http.socket.getaddrinfo", side_effect=_fake):
            _pin_httpx_request_to_safe_ip(request)

        assert request.url.host == pinned_ip
        assert request.url.path == "/foo"
        assert request.url.query == b"q=1"
        assert request.headers["Host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"

    def test_hook_blocks_imds_request(self) -> None:
        request = httpx.Request("GET", "https://attacker.example/x")

        def _fake(_host, _port, *_a, **_kw):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))
            ]

        with patch("fluid_build.util.safe_http.socket.getaddrinfo", side_effect=_fake):
            with pytest.raises(UnsafeURLError):
                _pin_httpx_request_to_safe_ip(request)


class TestSafeHttpxClient:
    """The factory wires the hook + default flags correctly."""

    def test_base_url_validated_at_construction(self) -> None:
        def _imds(_host, _port, *_a, **_kw):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))
            ]

        with patch("fluid_build.util.safe_http.socket.getaddrinfo", side_effect=_imds):
            with pytest.raises(UnsafeURLError):
                safe_httpx_client(base_url="https://attacker.test/")

    def test_require_https_rejects_http_base(self) -> None:
        # No DNS lookup needed — scheme check fires first.
        with pytest.raises(UnsafeURLError, match="non-https"):
            safe_httpx_client(
                base_url="http://api.example.com/", require_https=True
            )

    def test_follow_redirects_default_false(self) -> None:
        def _ok(_host, _port, *_a, **_kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.1.1.1", 0))]

        with patch("fluid_build.util.safe_http.socket.getaddrinfo", side_effect=_ok):
            client = safe_httpx_client(base_url="https://safe.test/")
        try:
            assert client.follow_redirects is False
        finally:
            client.close()

    def test_request_hook_runs_on_get(self) -> None:
        """End-to-end: build a client, mock the transport, fire a request,
        and verify the hook rewrote the URL to the pinned IP + set the
        Host header before the transport saw the request."""
        captured: dict = {}

        def _capture_transport(request: "httpx.Request") -> "httpx.Response":
            captured["url"] = str(request.url)
            captured["host"] = request.headers.get("Host")
            captured["sni"] = request.extensions.get("sni_hostname")
            return httpx.Response(200, text="ok")

        def _ok(_host, _port, *_a, **_kw):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
            ]

        with patch("fluid_build.util.safe_http.socket.getaddrinfo", side_effect=_ok):
            client = httpx.Client(
                transport=httpx.MockTransport(_capture_transport),
                event_hooks={"request": [_pin_httpx_request_to_safe_ip]},
                follow_redirects=False,
            )
            try:
                resp = client.get("https://example.com/probe")
            finally:
                client.close()

        assert resp.status_code == 200
        assert captured["host"] == "example.com"
        assert captured["sni"] == "example.com"
        # URL was rewritten to use the pinned IP, NOT the original hostname.
        assert "93.184.216.34" in captured["url"]
        assert "example.com" not in captured["url"]

    def test_per_request_url_revalidated(self) -> None:
        """A safe base_url + an unsafe per-request override must still
        be blocked at request time."""
        def _resolve(host, _port, *_a, **_kw):
            if host == "safe.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.1.1.1", 0))]
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))
            ]

        with patch(
            "fluid_build.util.safe_http.socket.getaddrinfo", side_effect=_resolve
        ):
            client = safe_httpx_client(base_url="https://safe.test/")
            try:
                with pytest.raises(UnsafeURLError):
                    client.get("https://attacker.test/x")
            finally:
                client.close()


class TestFetchBytes:
    """fetch_bytes — streaming GET with size cap, used by callers that
    need a raw bytes body (e.g. the ODPS resolver). The streaming
    + per-chunk cap guarantees we never buffer past the limit."""

    def test_blocks_non_http_scheme(self) -> None:
        from fluid_build.util.safe_http import fetch_bytes

        with pytest.raises(UnsafeURLError):
            fetch_bytes("ftp://example.com/x", timeout=1)

    def test_blocks_imds(self) -> None:
        from fluid_build.util.safe_http import fetch_bytes

        def _imds(_host, _port, *_a, **_kw):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))
            ]

        with patch("fluid_build.util.safe_http.socket.getaddrinfo", side_effect=_imds):
            with pytest.raises(UnsafeURLError):
                fetch_bytes("https://attacker.example/x", timeout=1)

    def test_size_cap_enforced_via_streaming(self) -> None:
        """Body larger than max_bytes must raise before buffering the
        full response — even if Content-Length lies. Asserted by mocking
        the transport with a multi-chunk body and a tiny cap."""
        from fluid_build.util.safe_http import (  # noqa: F401 — used in patch
            _pin_httpx_request_to_safe_ip,
            fetch_bytes,
        )

        large_body = b"x" * 1024
        called_chunks: dict = {"n": 0}

        def _mock_transport(request: "httpx.Request") -> "httpx.Response":
            called_chunks["n"] += 1
            return httpx.Response(200, content=large_body)

        def _ok(_host, _port, *_a, **_kw):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
            ]

        # Bypass real network by routing safe_httpx_client through a
        # MockTransport. We need to patch the factory's httpx.Client
        # construction.
        real_safe_httpx = safe_httpx_client

        def _patched_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_mock_transport)
            return real_safe_httpx(*args, **kwargs)

        with patch("fluid_build.util.safe_http.socket.getaddrinfo", side_effect=_ok), \
             patch("fluid_build.util.safe_http.safe_httpx_client", side_effect=_patched_factory):
            with pytest.raises(UnsafeURLError, match="exceeds"):
                fetch_bytes("https://safe.test/big", max_bytes=100)
