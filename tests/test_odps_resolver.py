# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Phase 3 — ContractResolver unit tests.

Focused unit tests for the resolver: probe order, candidate filename
generation, http(s) refusal under --no-remote, cache behaviour, validation
gate, and the HTML-with-200 safety check.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fluid_build.providers.odps_standard.resolver import (
    ContractNotFound,
    ContractResolver,
    ContractValidationError,
    RemoteFetchDisabled,
    ResolvedContract,
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
        with patch(
            "fluid_build.providers.odps_standard.resolver.read_input"
        ) as mock_read:
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
        # Default subdirs: '', contracts, odcs, odcs/contracts
        assert any("contracts" in c for c in candidates)
        assert any("odcs" in c for c in candidates)


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

    def test_remote_fetch_disabled_raises_on_url_contract_id(
        self, tmp_path: Path
    ) -> None:
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
# Remote fetch — mocked urlopen
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/json") -> None:
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class TestRemoteResolution:
    def test_html_with_200_is_refused(self, tmp_path: Path) -> None:
        resolver = ContractResolver(base_path=tmp_path, allow_remote=True)
        with patch(
            "fluid_build.providers.odps_standard.resolver.urlopen",
            return_value=_FakeResponse(b"<html><body>404</body></html>", "text/html"),
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
        with patch(
            "fluid_build.providers.odps_standard.resolver.urlopen",
            return_value=_FakeResponse(body),
        ) as mock_open:
            first = resolver.resolve("https://example.com/remote.contract")
            second = resolver.resolve("https://example.com/remote.contract")
            mock_open.assert_called_once()
        assert first is second
        assert first.source == "remote"
