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

"""Cross-mesh federation skeleton — pin manifest parsing + cache layer
+ violation contract.

The live-fetch backends (gitpython, REST clients) are intentionally
not wired in this skeleton — wiring is per-integration follow-up
work. Tests here cover the surface that IS production-ready:

* Manifest parsing from ``federation/upstreams.yaml``.
* Per-workspace digest cache read / write.
* Violation surfacing when the manifest is missing / digest is
  unpinned / live fetcher isn't wired yet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fluid_build.forge.federation import (
    FederatedConsumeViolation,
    FederatedWorkspace,
    FederationManifest,
    fetch_federated_digest,
    get_cached_digest,
    load_federation_manifest,
    store_cached_digest,
    validate_federated_consumes,
)


def _write_manifest(workspace: Path, payload: dict) -> Path:
    path = workspace / "federation" / "upstreams.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload))
    return path


class TestLoadFederationManifest:
    def test_missing_file_returns_empty(self, tmp_path: Path):
        m = load_federation_manifest(tmp_path)
        assert m.workspaces == []

    def test_parses_well_formed_manifest(self, tmp_path: Path):
        _write_manifest(
            tmp_path,
            {
                "workspaces": [
                    {
                        "id": "telco",
                        "kind": "git_registry",
                        "endpoint": "https://github.com/acme/telco",
                        "auth": {"mode": "github_token", "secret_ref": "GH_TOKEN"},
                    },
                    {
                        "id": "marketing",
                        "kind": "catalog",
                        "endpoint": "https://catalog.acme/api",
                    },
                ]
            },
        )
        m = load_federation_manifest(tmp_path)
        assert len(m.workspaces) == 2
        ws = m.get("telco")
        assert ws is not None
        assert ws.kind == "git_registry"
        assert ws.auth_mode == "github_token"
        assert ws.auth_secret_ref == "GH_TOKEN"

    def test_invalid_yaml_logs_and_returns_empty(self, tmp_path: Path):
        path = tmp_path / "federation" / "upstreams.yaml"
        path.parent.mkdir()
        path.write_text("not: valid: yaml: [\n")
        m = load_federation_manifest(tmp_path)
        assert m.workspaces == []


class TestDigestCache:
    def test_read_empty_returns_none(self, tmp_path: Path):
        assert get_cached_digest(tmp_path, "telco", "orders_v1", "1") is None

    def test_write_then_read_round_trips(self, tmp_path: Path):
        store_cached_digest(tmp_path, "telco", "orders_v1", "1", "sha256:abc")
        assert get_cached_digest(tmp_path, "telco", "orders_v1", "1") == "sha256:abc"

    def test_write_persists_metadata(self, tmp_path: Path):
        store_cached_digest(tmp_path, "telco", "p1", "1", "sha256:1")
        cache_file = tmp_path / ".fluid" / "federation" / "telco.digest-cache.json"
        cache = json.loads(cache_file.read_text())
        assert "__updated_at" in cache
        assert "Z" in cache["__updated_at"]


class TestValidateFederatedConsumes:
    def test_empty_consumes_no_violations(self, tmp_path: Path):
        violations = validate_federated_consumes({"consumes": []}, workspace_root=tmp_path)
        assert violations == []

    def test_local_consumes_pass_through(self, tmp_path: Path):
        """Entries without ``upstreamWorkspace`` are local — handled
        by the in-workspace validator, NOT this one."""
        violations = validate_federated_consumes(
            {"consumes": [{"productId": "local.orders", "exposeId": "orders"}]},
            workspace_root=tmp_path,
        )
        assert violations == []

    def test_unpinned_federated_consume_violates(self, tmp_path: Path):
        _write_manifest(
            tmp_path,
            {
                "workspaces": [
                    {
                        "id": "telco",
                        "kind": "git_registry",
                        "endpoint": "https://example.com",
                    }
                ]
            },
        )
        contract = {
            "consumes": [
                {
                    "productId": "telco.orders",
                    "upstreamWorkspace": "telco",
                    # NOTE: no upstreamDigest
                }
            ]
        }
        violations = validate_federated_consumes(contract, workspace_root=tmp_path)
        assert len(violations) == 1
        assert "missing required" in violations[0].reason

    def test_unknown_workspace_violates(self, tmp_path: Path):
        # No manifest written, so ``unknown`` is, well, unknown.
        contract = {
            "consumes": [
                {
                    "productId": "telco.orders",
                    "upstreamWorkspace": "unknown",
                    "upstreamDigest": "sha256:abc",
                }
            ]
        }
        violations = validate_federated_consumes(contract, workspace_root=tmp_path)
        assert len(violations) == 1
        assert "not declared" in violations[0].reason

    def test_unwired_fetcher_surfaces_as_violation(self, tmp_path: Path):
        """Skeleton-mode: the live-fetch backend raises
        NotImplementedError, which the validator converts to a
        violation so apply doesn't silently accept the unverified
        digest. Wiring the real fetcher converts this to a real
        compare path."""
        _write_manifest(
            tmp_path,
            {
                "workspaces": [
                    {
                        "id": "telco",
                        "kind": "git_registry",
                        "endpoint": "https://example.com",
                    }
                ]
            },
        )
        contract = {
            "consumes": [
                {
                    "productId": "telco.orders",
                    "upstreamWorkspace": "telco",
                    "upstreamDigest": "sha256:abc",
                }
            ]
        }
        violations = validate_federated_consumes(contract, workspace_root=tmp_path)
        assert len(violations) == 1
        assert "not yet wired" in violations[0].reason

    def test_cached_digest_short_circuits_to_compare(self, tmp_path: Path):
        """When the cache has the digest, the validator skips the
        live-fetch path entirely. Cached drift is real drift."""
        _write_manifest(
            tmp_path,
            {
                "workspaces": [
                    {
                        "id": "telco",
                        "kind": "git_registry",
                        "endpoint": "https://example.com",
                    }
                ]
            },
        )
        store_cached_digest(tmp_path, "telco", "telco.orders", "1", "sha256:LIVE")
        contract = {
            "consumes": [
                {
                    "productId": "telco.orders",
                    "upstreamWorkspace": "telco",
                    "upstreamDigest": "sha256:STALE",  # different from cache
                }
            ]
        }
        violations = validate_federated_consumes(contract, workspace_root=tmp_path)
        assert len(violations) == 1
        assert "drift" in violations[0].reason
        assert violations[0].expected_digest == "sha256:STALE"
        assert violations[0].actual_digest == "sha256:LIVE"


class TestFetchFederatedDigest:
    def test_cache_hit_returns_immediately(self, tmp_path: Path):
        ws = FederatedWorkspace(id="telco", kind="git_registry", endpoint="https://x")
        store_cached_digest(tmp_path, "telco", "p1", "1", "sha256:cached")
        result = fetch_federated_digest(ws, "p1", "1", workspace_root=tmp_path)
        assert result == "sha256:cached"

    def test_no_cache_unreachable_endpoint_raises(self, tmp_path: Path):
        """When the cache misses AND the live fetch fails (unreachable
        endpoint, missing repo), :func:`fetch_federated_digest` raises
        ``NotImplementedError`` with diagnostics. This pins the failure
        contract callers (validators) depend on."""
        ws = FederatedWorkspace(id="telco", kind="git_registry", endpoint="https://x")
        with pytest.raises(NotImplementedError, match="kind=git_registry"):
            fetch_federated_digest(ws, "p1", "1", workspace_root=tmp_path)

    def test_unrecognised_kind_raises(self, tmp_path: Path):
        """Unknown ``kind`` values surface as a typed error so the
        manifest author gets actionable feedback instead of a silent
        fallthrough."""
        ws = FederatedWorkspace(id="telco", kind="some_new_kind", endpoint="https://x")
        with pytest.raises(NotImplementedError, match="not a recognised backend"):
            fetch_federated_digest(ws, "p1", "1", workspace_root=tmp_path)
