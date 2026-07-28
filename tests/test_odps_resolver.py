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

"""Phase 3 — ContractResolver unit tests.

Focused unit tests for the resolver: probe order, candidate filename
generation, http(s) refusal under --no-remote, cache behaviour, validation
gate, and the HTML-with-200 safety check.
"""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from fluid_build.providers.odps_standard.resolver import (
    ContractNotFound,
    ContractResolver,
    ContractValidationError,
    RemoteFetchDisabled,
    ResolvedContract,
    UnsafeURLError,
    _assert_safe_url,
    _looks_like_url,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "odps" / "product-bitol"


def _first_odcs_id() -> str:
    """The contractId baked into the fixture's daily_orders ODCS doc."""
    return "commerce.orders-product.daily_orders"


# ---------------------------------------------------------------------------
# Local file resolution
# ---------------------------------------------------------------------------


class TestLocalResolution:
    def test_resolves_from_base_path_with_default_extensions(self) -> None:
        resolver = ContractResolver(base_path=FIXTURE_DIR, allow_remote=False)
        result = resolver.resolve(_first_odcs_id())
        assert result.source == "local"
        assert result.odcs["id"] == _first_odcs_id()

    def test_index_directory_makes_lookups_first_hit(self) -> None:
        resolver = ContractResolver(base_path=FIXTURE_DIR, allow_remote=False)
        resolver.index_directory(FIXTURE_DIR)
        result = resolver.resolve(_first_odcs_id())
        # Confirm the resolver took the index path, not the candidate probes
        assert Path(result.origin).parent == FIXTURE_DIR

    def test_cache_hit_avoids_re_read(self) -> None:
        resolver = ContractResolver(base_path=FIXTURE_DIR, allow_remote=False)
        first = resolver.resolve(_first_odcs_id())
        with patch("fluid_build.providers.odps_standard.resolver.read_input") as mock_read:
            second = resolver.resolve(_first_odcs_id())
            mock_read.assert_not_called()
        assert first is second


class TestCandidateFilenames:
    def test_local_candidates_probe_full_id_and_last_segment(self, tmp_path: Path) -> None:
        resolver = ContractResolver(base_path=tmp_path, allow_remote=False)
        candidates = [str(c) for c in resolver._local_candidates("foo.bar.baz")]
        # Both the full id and the last segment must appear
        assert any("foo.bar.baz" in c for c in candidates)
        assert any("baz" in c and "foo.bar.baz" not in c for c in candidates)

    def test_local_candidates_include_default_subdirs(self, tmp_path: Path) -> None:
        resolver = ContractResolver(base_path=tmp_path, allow_remote=False)
        candidates = [str(c) for c in resolver._local_candidates("id")]
        # Default subdirs (trimmed from the original 4-way matrix to 2):
        # '' (the base_path itself) and 'contracts/'.
        assert any(c.endswith("id.odcs.yaml") for c in candidates)
        assert any(f"contracts{Path('/').as_posix()}id" in c for c in candidates)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrors:
    def test_contract_not_found_lists_first_5_candidates(self, tmp_path: Path) -> None:
        resolver = ContractResolver(base_path=tmp_path, allow_remote=False)
        with pytest.raises(ContractNotFound) as exc_info:
            resolver.resolve("missing.contract")
        # The error message must include candidates tried but cap at 5 + summary
        msg = str(exc_info.value)
        assert "missing.contract" in msg
        assert "Tried:" in msg

    def test_remote_fetch_disabled_raises_on_url_contract_id(self, tmp_path: Path) -> None:
        resolver = ContractResolver(base_path=tmp_path, allow_remote=False)
        with pytest.raises(RemoteFetchDisabled):
            resolver.resolve("https://example.com/contract.odcs.yaml")

    def test_url_hint_with_remote_disabled_raises(self, tmp_path: Path) -> None:
        resolver = ContractResolver(base_path=tmp_path, allow_remote=False)
        with pytest.raises(RemoteFetchDisabled):
            resolver.resolve("anything", hint="https://example.com/c.yaml")


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestLooksLikeUrl:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("https://example.com/c.yaml", True),
            ("http://example.com/c.yaml", True),
            ("ftp://example.com/c.yaml", False),  # not http(s)
            ("file:///tmp/c.yaml", False),
            ("just-an-id", False),
            ("product.something", False),
            ("https://", False),  # no netloc
        ],
    )
    def test_detection(self, value: str, expected: bool) -> None:
        assert _looks_like_url(value) is expected


# ---------------------------------------------------------------------------
# Remote fetch — mocked _fetch_bytes (the httpx-based safe helper)
# ---------------------------------------------------------------------------


class TestRemoteResolution:
    def test_html_with_200_is_refused(self, tmp_path: Path) -> None:
        resolver = ContractResolver(base_path=tmp_path, allow_remote=True)
        with (
            patch(
                "fluid_build.providers.odps_standard.resolver._fetch_bytes",
                return_value=(
                    200,
                    {"content-type": "text/html"},
                    b"<html><body>404</body></html>",
                ),
            ),
            patch(
                "fluid_build.providers.odps_standard.resolver._assert_safe_url",
                return_value=None,
            ),
        ):
            with pytest.raises(ContractNotFound):
                resolver.resolve("https://example.com/c.odcs.yaml")

    def test_successful_remote_caches_result(self, tmp_path: Path) -> None:
        import json

        body = json.dumps(
            {
                "version": "1.0.0",
                "apiVersion": "v3.1.0",
                "kind": "DataContract",
                "id": "remote.contract",
                "status": "active",
                "schema": [],
                "servers": [],
            }
        ).encode()
        resolver = ContractResolver(base_path=tmp_path, allow_remote=True)
        with (
            patch(
                "fluid_build.providers.odps_standard.resolver._fetch_bytes",
                return_value=(200, {"content-type": "application/json"}, body),
            ) as mock_fetch,
            patch(
                "fluid_build.providers.odps_standard.resolver._assert_safe_url",
                return_value=None,
            ),
        ):
            first = resolver.resolve("https://example.com/remote.contract")
            second = resolver.resolve("https://example.com/remote.contract")
            mock_fetch.assert_called_once()
        assert first is second
        assert first.source == "remote"


# ---------------------------------------------------------------------------
# SSRF guard — _assert_safe_url + resolver behaviour on unsafe URLs
# ---------------------------------------------------------------------------


class TestSSRFGuard:
    """Pin the SSRF-protection behaviour added after the May 2026
    security review. _assert_safe_url must reject every common
    internal-address shape and any non-http(s) scheme; the resolver
    must surface those rejections as ContractNotFound (not silent fetch)
    and must NOT leak fetched body fragments into validation errors.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "gopher://example.com/",
            "ftp://example.com/x",
            "jar:https://example.com/x!/",
        ],
    )
    def test_rejects_non_http_scheme(self, url: str) -> None:
        with pytest.raises(UnsafeURLError):
            _assert_safe_url(url)

    @pytest.mark.parametrize(
        "host, addr",
        [
            ("imds.test", "169.254.169.254"),  # AWS / GCP / Azure metadata
            ("loop.test", "127.0.0.1"),
            ("loop6.test", "::1"),
            ("rfc1918.test", "10.0.0.5"),
            ("rfc1918b.test", "172.16.1.1"),
            ("rfc1918c.test", "192.168.1.1"),
            ("linklocal.test", "169.254.10.10"),
        ],
    )
    def test_rejects_non_public_address(self, host: str, addr: str) -> None:
        # Stub DNS so we can deterministically map our test hostnames to
        # the addresses we want to verify the filter against, without
        # actually hitting the network or relying on the host's resolver.
        def _fake_getaddrinfo(_host, _port, *_a, **_kw):
            family = socket.AF_INET6 if ":" in addr else socket.AF_INET
            return [(family, socket.SOCK_STREAM, 0, "", (addr, 0))]

        with patch(
            "fluid_build.util.safe_http.socket.getaddrinfo",
            side_effect=_fake_getaddrinfo,
        ):
            with pytest.raises(UnsafeURLError):
                _assert_safe_url(f"https://{host}/x")

    @pytest.mark.parametrize(
        "host, addr",
        [
            # IPv4-mapped IPv6 — bypass on Python <3.12 absent the
            # ipv4_mapped unwrap. Pinning loopback + IMDS + RFC1918 via
            # the ::ffff: prefix.
            ("v4mapped-imds.test", "::ffff:169.254.169.254"),
            ("v4mapped-loop.test", "::ffff:127.0.0.1"),
            ("v4mapped-rfc1918.test", "::ffff:10.0.0.1"),
            # CIDRs ipaddress.is_private does NOT flag — borrowed from
            # requests-hardened (Saleor, BSD-3).
            ("cgnat.test", "100.64.0.5"),  # RFC 6598 carrier-grade NAT
            ("six-to-four.test", "192.88.99.10"),  # RFC 7526 6to4 relay
            ("benchmark.test", "198.18.0.1"),  # RFC 2544
            ("test-net-1.test", "192.0.2.5"),  # RFC 5737 TEST-NET-1
            ("test-net-2.test", "198.51.100.5"),  # RFC 5737 TEST-NET-2
            ("test-net-3.test", "203.0.113.5"),  # RFC 5737 TEST-NET-3
            ("class-e.test", "240.0.0.1"),  # RFC 1112 class E reserved
            ("nat64.test", "64:ff9b::1.2.3.4"),  # RFC 6052
            ("six-to-four-v6.test", "2002::1"),  # RFC 3056 6to4
            ("orchid.test", "2001:20::1"),  # RFC 7343 ORCHIDv2
            ("ipv6-sr.test", "5f00::1"),  # RFC 9602 IPv6 SR
        ],
    )
    def test_rejects_extended_cidr_blocklist(self, host: str, addr: str) -> None:
        def _fake_getaddrinfo(_host, _port, *_a, **_kw):
            family = socket.AF_INET6 if ":" in addr else socket.AF_INET
            return [(family, socket.SOCK_STREAM, 0, "", (addr, 0))]

        with patch(
            "fluid_build.util.safe_http.socket.getaddrinfo",
            side_effect=_fake_getaddrinfo,
        ):
            with pytest.raises(UnsafeURLError):
                _assert_safe_url(f"https://{host}/x")

    def test_index_refuses_url_shaped_id(self, tmp_path: Path) -> None:
        """A local file with id: 'https://...' must not be indexed.
        Otherwise a low-trust file dropped into the workspace could
        pre-empt a remote fetch later (cache-poison across allow_remote
        flips)."""
        import yaml as _yaml

        bad = tmp_path / "poison.odcs.yaml"
        bad.write_text(
            _yaml.dump(
                {
                    "apiVersion": "v3.1.0",
                    "kind": "DataContract",
                    "id": "https://attacker.example/contract",
                    "name": "Poison",
                    "version": "1.0.0",
                    "status": "active",
                    "schema": [],
                }
            )
        )
        resolver = ContractResolver(base_path=tmp_path, allow_remote=False)
        resolver.index_directory(tmp_path)
        assert "https://attacker.example/contract" not in resolver._index

    def test_absolute_path_contract_id_does_not_escape_base(self, tmp_path: Path) -> None:
        """A poisoned contractId beginning with '/' must NOT cause the
        resolver to probe outside ``base_path``. ``Path("base") /
        "/etc/hostname"`` silently discards the base — guarded here."""
        resolver = ContractResolver(base_path=tmp_path, allow_remote=False)
        candidates = resolver._local_candidates("/etc/hostname")
        # Every produced candidate must be a descendant of base_path.
        base_resolved = tmp_path.resolve()
        for c in candidates:
            try:
                c.resolve().relative_to(base_resolved)
            except ValueError:
                pytest.fail(f"candidate {c} escapes base_path {tmp_path}")

    def test_allows_public_address(self) -> None:
        def _fake_getaddrinfo(_host, _port, *_a, **_kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]

        with patch(
            "fluid_build.util.safe_http.socket.getaddrinfo",
            side_effect=_fake_getaddrinfo,
        ):
            # Should not raise
            _assert_safe_url("https://example.com/contract.odcs.yaml")

    def test_resolver_surfaces_unsafe_url_as_contract_not_found(self, tmp_path: Path) -> None:
        """A poisoned contractId pointing at IMDS must NOT make the
        request. The resolver translates the SSRF rejection into a
        ContractNotFound so the caller treats it as a missing reference
        rather than leaking the failure reason upstream."""
        resolver = ContractResolver(base_path=tmp_path, allow_remote=True)

        def _fake_getaddrinfo(_host, _port, *_a, **_kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))]

        with (
            patch(
                "fluid_build.util.safe_http.socket.getaddrinfo",
                side_effect=_fake_getaddrinfo,
            ),
            patch("fluid_build.providers.odps_standard.resolver._fetch_bytes") as mock_fetch,
        ):
            with pytest.raises(ContractNotFound):
                resolver.resolve("https://attacker.example/redir")
            # _fetch_bytes must NOT have been reached — the resolver's
            # defence-in-depth assert_safe_url call rejected the URL
            # before any network I/O happened.
            mock_fetch.assert_not_called()

    def test_rejects_mixed_public_and_private_dns_answers(self) -> None:
        """If the hostname resolves to BOTH a public and a private
        address (e.g. attacker mixes a public A record with a private
        AAAA), the entire lookup must fail — no cherry-picking."""

        def _mixed(_host, _port, *_a, **_kw):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0)),
            ]

        with patch(
            "fluid_build.util.safe_http.socket.getaddrinfo",
            side_effect=_mixed,
        ):
            with pytest.raises(UnsafeURLError):
                _assert_safe_url("https://mixed.test/x")

    def test_body_size_cap_rejects_oversized_remote(self, tmp_path: Path) -> None:
        """A response larger than MAX_REMOTE_BYTES is dropped before
        parsing. The cap fires inside safe_http.fetch_bytes; the resolver
        translates the resulting UnsafeURLError into ContractNotFound."""
        resolver = ContractResolver(base_path=tmp_path, allow_remote=True)
        with (
            patch(
                "fluid_build.providers.odps_standard.resolver._fetch_bytes",
                side_effect=UnsafeURLError("response exceeds size cap"),
            ),
            patch(
                "fluid_build.providers.odps_standard.resolver._assert_safe_url",
                return_value=("h", "1.1.1.1"),
            ),
        ):
            with pytest.raises(ContractNotFound):
                resolver.resolve("https://example.com/big")

    def test_validation_error_omits_remote_body(self, tmp_path: Path) -> None:
        """A remote 200 that parses to a Mapping but fails ODCS schema
        validation must not surface the offending field value in the
        error message. Pre-fix, the inner jsonschema message — which
        echoes the body — leaked through console_error + logger.error."""
        import json

        body = json.dumps(
            {
                # Missing required fields (no apiVersion/kind/id/version/status)
                "ssrf_canary": "ZZ_LEAKED_RESPONSE_TOKEN_ZZ",
            }
        ).encode()
        resolver = ContractResolver(base_path=tmp_path, allow_remote=True)
        with (
            patch(
                "fluid_build.providers.odps_standard.resolver._fetch_bytes",
                return_value=(200, {"content-type": "application/json"}, body),
            ),
            patch(
                "fluid_build.providers.odps_standard.resolver._assert_safe_url",
                return_value=None,
            ),
        ):
            with pytest.raises(ContractValidationError) as exc_info:
                resolver.resolve("https://example.com/c")
        # The remote body token must not appear in the error message.
        assert "ZZ_LEAKED_RESPONSE_TOKEN_ZZ" not in str(exc_info.value)
        # __cause__ must be cleared so the body doesn't leak via the
        # standard Python "The above exception was the direct cause of"
        # chained-traceback path either.
        assert exc_info.value.__cause__ is None
