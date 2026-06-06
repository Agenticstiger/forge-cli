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

"""Regression tests for the federation unbounded-read OOM fixes.

FINDING 3 (MEDIUM/LOW): ``fluid_build/forge/federation.py``
(a) ``_federation_http_get`` used ``httpx.Client().get()`` which buffers
    the WHOLE response body before ``.json()``/``.text`` — a multi-GB body
    OOMs the stage-7 digest gate of ``fluid apply``. The fix streams the
    body with the same per-chunk ceiling (``MAX_REMOTE_BYTES``) that
    ``safe_http.fetch_bytes`` uses, returning ``None`` once the cap trips.
(b) ``_read_first_existing_contract`` did ``resolved.read_text()`` with no
    size guard before ``load_yaml_safe``'s post-hoc 5 MiB cap. The fix
    stat-and-caps BEFORE the read using the shared ``MAX_YAML_BYTES``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from fluid_build.forge.federation import (
    FederatedWorkspace,
    _fetch_digest_via_http,
    _read_first_existing_contract,
)
from fluid_build.util.safe_http import MAX_REMOTE_BYTES
from fluid_build.util.safe_yaml import MAX_YAML_BYTES

_PUBLIC = "fluid_build.forge.federation._hostname_is_private"


def _ws(**overrides) -> FederatedWorkspace:
    defaults = dict(id="external", kind="http_registry", endpoint="https://registry.example/api")
    defaults.update(overrides)
    return FederatedWorkspace(**defaults)


# ───────────────────── (a) HTTP body-size cap ───────────────────────────


class TestFederationHttpBodyCap:
    @respx.mock
    def test_oversized_body_returns_none(self):
        """A >cap response body is rejected (capped) rather than buffered."""
        oversized = "sha256:" + ("A" * (MAX_REMOTE_BYTES + 1024))
        respx.get("https://registry.example/api/p/1/digest").mock(
            return_value=httpx.Response(200, text=oversized)
        )
        with patch(_PUBLIC, return_value=False):
            result = _fetch_digest_via_http(_ws(), "p", "1")
        assert result is None

    @respx.mock
    def test_just_under_cap_still_reads(self):
        """Positive control: a body just under the cap is read in full.

        The digest backend ``.strip()``s the whole body, so we pad the
        digest with trailing whitespace that stays just under the cap —
        proving the streamed read assembled the entire (capped) body, not
        a truncated prefix."""
        body = "sha256:abc123" + (" " * (MAX_REMOTE_BYTES - 64))
        assert len(body.encode("utf-8")) < MAX_REMOTE_BYTES
        respx.get("https://registry.example/api/p/1/digest").mock(
            return_value=httpx.Response(200, text=body)
        )
        with patch(_PUBLIC, return_value=False):
            result = _fetch_digest_via_http(_ws(), "p", "1")
        assert result == "sha256:abc123"

    @respx.mock
    def test_small_body_happy_path(self):
        respx.get("https://registry.example/api/p/1/digest").mock(
            return_value=httpx.Response(200, text="sha256:deadbeef\n")
        )
        with patch(_PUBLIC, return_value=False):
            result = _fetch_digest_via_http(_ws(), "p", "1")
        assert result == "sha256:deadbeef"


# ───────────────── (b) git-contract stat-before-read cap ─────────────────


class TestFederationGitContractSizeCap:
    def test_oversized_contract_file_rejected(self, tmp_path):
        """A contract file larger than MAX_YAML_BYTES is rejected by the
        stat-before-read guard (returns None, never read into memory)."""
        cache_dir = tmp_path / "clone"
        product_dir = cache_dir / "orders_v1"
        product_dir.mkdir(parents=True)
        contract = product_dir / "contract.fluid.yaml"
        # Write a file one byte over the cap.
        contract.write_bytes(b"a: 1\n" + b"#" * (MAX_YAML_BYTES + 1))
        assert contract.stat().st_size > MAX_YAML_BYTES

        ws = _ws(id="orders-mesh", kind="git_registry")
        result = _read_first_existing_contract(cache_dir, ws, "orders_v1")
        assert result is None

    def test_in_bounds_contract_is_read(self, tmp_path):
        """Positive control: a normal-sized contract is read."""
        cache_dir = tmp_path / "clone"
        product_dir = cache_dir / "orders_v1"
        product_dir.mkdir(parents=True)
        (product_dir / "contract.fluid.yaml").write_text("id: orders_v1\nname: Orders\n")

        ws = _ws(id="orders-mesh", kind="git_registry")
        result = _read_first_existing_contract(cache_dir, ws, "orders_v1")
        assert result is not None
        assert "orders_v1" in result
