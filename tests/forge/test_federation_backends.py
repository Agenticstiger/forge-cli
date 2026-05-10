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

"""Pin federation live-fetch backends.

Three backends, three tests each:

* ``http_registry`` — plain HTTP GET ``<endpoint>/<product>/<version>/digest``
  returning ``sha256:...``.
* ``catalog`` — REST GET returning JSON ``{"digest": "..."}``.
* ``git_registry`` — clone repo, read contract.fluid.yaml, compute digest.

Each test exercises:
1. Happy path → returns expected digest.
2. Network/HTTP failure → returns None (validators surface as violation).
3. Auth header construction → secret_ref env var resolved correctly.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.forge.federation import (
    FederatedWorkspace,
    _fetch_digest_via_catalog,
    _fetch_digest_via_git,
    _fetch_digest_via_http,
    fetch_federated_digest,
    store_cached_digest,
)

# ──────────────────── HTTP registry backend ────────────────────────────


class TestHttpBackend:
    def _ws(self, **overrides) -> FederatedWorkspace:
        defaults = dict(
            id="external",
            kind="http_registry",
            endpoint="https://registry.example/api",
        )
        defaults.update(overrides)
        return FederatedWorkspace(**defaults)

    def test_happy_path_returns_digest(self):
        ws = self._ws()
        fake_resp = MagicMock()
        fake_resp.read.return_value = b"sha256:abc123\n"
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = _fetch_digest_via_http(ws, "orders_v1", "1")
        assert result == "sha256:abc123"

    def test_bearer_auth_header_built_from_secret_ref(self, monkeypatch):
        ws = self._ws(auth_mode="bearer", auth_secret_ref="REGISTRY_TOKEN")
        monkeypatch.setenv("REGISTRY_TOKEN", "tok-9876")

        captured: Dict[str, Any] = {}

        def fake_urlopen(req, timeout=15):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            resp = MagicMock()
            resp.read.return_value = b"sha256:def\n"
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            return resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = _fetch_digest_via_http(ws, "p", "1")
        assert result == "sha256:def"
        # Header keys are normalised to title-case by urllib.
        assert captured["headers"].get("Authorization") == "Bearer tok-9876"

    def test_http_error_returns_none(self):
        import urllib.error

        ws = self._ws()
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                url="x", code=403, msg="forbidden", hdrs=None, fp=None
            ),
        ):
            result = _fetch_digest_via_http(ws, "p", "1")
        assert result is None

    def test_unexpected_body_returns_none(self):
        ws = self._ws()
        fake_resp = MagicMock()
        fake_resp.read.return_value = b"<html>not a digest</html>"
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = _fetch_digest_via_http(ws, "p", "1")
        assert result is None


# ──────────────────── Catalog backend ───────────────────────────────────


class TestCatalogBackend:
    def _ws(self, **overrides) -> FederatedWorkspace:
        defaults = dict(
            id="cat",
            kind="catalog",
            endpoint="https://catalog.example/api",
        )
        defaults.update(overrides)
        return FederatedWorkspace(**defaults)

    def test_happy_path_returns_digest(self):
        ws = self._ws()
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({"digest": "sha256:cat"}).encode()
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = _fetch_digest_via_catalog(ws, "orders", "1")
        assert result == "sha256:cat"

    def test_missing_digest_field_returns_none(self):
        ws = self._ws()
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({"name": "orders"}).encode()
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = _fetch_digest_via_catalog(ws, "orders", "1")
        assert result is None

    def test_404_returns_none(self):
        import urllib.error

        ws = self._ws()
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(url="x", code=404, msg="nf", hdrs=None, fp=None),
        ):
            result = _fetch_digest_via_catalog(ws, "orders", "1")
        assert result is None


# ──────────────────── Git backend ───────────────────────────────────────


class TestGitBackend:
    """The git backend reads a contract via gitpython / shell-out then
    hashes it. Patch :func:`_git_read_contract` so we don't need a real
    repo on disk for the happy path."""

    def _ws(self, **overrides) -> FederatedWorkspace:
        defaults = dict(
            id="ext",
            kind="git_registry",
            endpoint="https://github.example/foo",
        )
        defaults.update(overrides)
        return FederatedWorkspace(**defaults)

    def test_happy_path_uses_compute_contract_digest(self):
        ws = self._ws()
        contract_text = "fluidVersion: 0.7.3\nid: external.orders\nexposes:\n  - id: orders\n"
        with patch(
            "fluid_build.forge.federation._git_read_contract",
            return_value=contract_text,
        ):
            result = _fetch_digest_via_git(ws, "external.orders", "1")
        assert result is not None and result.startswith("sha256:")

    def test_missing_repo_returns_none(self):
        ws = self._ws()
        with patch("fluid_build.forge.federation._git_read_contract", return_value=None):
            result = _fetch_digest_via_git(ws, "external.orders", "1")
        assert result is None

    def test_gitpython_path_is_tried_first(self, tmp_path: Path, monkeypatch):
        """When gitpython is installed, ``_git_clone_or_pull_via_gitpython``
        is called BEFORE the shell-out fallback. Pin the dispatch order
        so a gitpython regression doesn't silently fall through to
        shell-out (which has different error semantics)."""
        from fluid_build.forge import federation as _fed

        ws = FederatedWorkspace(id="ext-gp", kind="git_registry", endpoint="https://example.com/r")

        # Force the cache dir into tmp_path so the test doesn't clobber
        # ~/.cache/fluid/federation-git on the developer's box.
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(tmp_path / "home_cache") if "~" in p else p,
        )

        with (
            patch(
                "fluid_build.forge.federation._git_clone_or_pull_via_gitpython",
                return_value=True,
            ) as mock_gp,
            patch("fluid_build.forge.federation._git_clone_or_pull_via_shellout") as mock_sh,
            patch(
                "fluid_build.forge.federation._read_first_existing_contract",
                return_value="fluidVersion: 0.7.3\nid: ext.x\n",
            ),
        ):
            result = _fetch_digest_via_git(ws, "ext.x", "1")

        assert mock_gp.called, "gitpython path must be tried first"
        assert not mock_sh.called, "shell-out fallback must NOT be called when gitpython succeeds"
        assert result is not None and result.startswith("sha256:")

    def test_shellout_fallback_when_gitpython_unavailable(self, tmp_path: Path, monkeypatch):
        """When gitpython returns None (not installed), shell-out
        fallback runs. Pin the fall-through path so an environment
        without gitpython still resolves federated digests."""
        ws = FederatedWorkspace(id="ext-sh", kind="git_registry", endpoint="https://example.com/r")
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(tmp_path / "home_cache") if "~" in p else p,
        )

        with (
            patch(
                "fluid_build.forge.federation._git_clone_or_pull_via_gitpython",
                return_value=None,
            ) as mock_gp,
            patch(
                "fluid_build.forge.federation._git_clone_or_pull_via_shellout",
                return_value=True,
            ) as mock_sh,
            patch(
                "fluid_build.forge.federation._read_first_existing_contract",
                return_value="fluidVersion: 0.7.3\nid: ext.x\n",
            ),
        ):
            result = _fetch_digest_via_git(ws, "ext.x", "1")

        assert mock_gp.called and mock_sh.called, (
            "Both gitpython AND shell-out must be exercised when gitpython is unavailable"
        )
        assert result is not None and result.startswith("sha256:")

    def test_gitpython_real_failure_aborts_no_shellout(self, tmp_path: Path, monkeypatch):
        """When gitpython is installed but the clone fails (auth, dead
        remote, etc.), we MUST NOT fall through to shell-out — the
        failure mode would be identical and the operator should see
        the gitpython error."""
        ws = FederatedWorkspace(
            id="ext-gp-fail", kind="git_registry", endpoint="https://example.com/r"
        )
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(tmp_path / "home_cache") if "~" in p else p,
        )

        with (
            patch(
                "fluid_build.forge.federation._git_clone_or_pull_via_gitpython",
                return_value=False,
            ) as mock_gp,
            patch("fluid_build.forge.federation._git_clone_or_pull_via_shellout") as mock_sh,
        ):
            result = _fetch_digest_via_git(ws, "ext.x", "1")

        assert mock_gp.called and not mock_sh.called, (
            "gitpython failure must NOT trigger shell-out fallback"
        )
        assert result is None


# ──────────────────── End-to-end: cache + dispatch ─────────────────────


class TestFetchFederatedDigestDispatch:
    def test_cache_short_circuits_live_fetch(self, tmp_path: Path):
        ws = FederatedWorkspace(id="ext", kind="http_registry", endpoint="https://x")
        store_cached_digest(tmp_path, "ext", "p1", "1", "sha256:cached")

        with patch("fluid_build.forge.federation._fetch_digest_via_http") as live:
            live.side_effect = AssertionError("live fetch should be skipped")
            result = fetch_federated_digest(ws, "p1", "1", workspace_root=tmp_path)
        assert result == "sha256:cached"
        assert not live.called

    def test_live_fetch_persists_to_cache(self, tmp_path: Path):
        ws = FederatedWorkspace(id="ext", kind="http_registry", endpoint="https://x")
        with patch(
            "fluid_build.forge.federation._fetch_digest_via_http",
            return_value="sha256:fresh",
        ):
            result = fetch_federated_digest(ws, "p1", "1", workspace_root=tmp_path)
        assert result == "sha256:fresh"

        # Second call hits cache, doesn't re-invoke live fetch.
        with patch("fluid_build.forge.federation._fetch_digest_via_http") as live:
            live.side_effect = AssertionError("should hit cache")
            result2 = fetch_federated_digest(ws, "p1", "1", workspace_root=tmp_path)
        assert result2 == "sha256:fresh"

    def test_dispatch_routes_to_correct_backend(self, tmp_path: Path):
        ws_http = FederatedWorkspace(id="a", kind="http_registry", endpoint="https://x")
        ws_cat = FederatedWorkspace(id="b", kind="catalog", endpoint="https://x")
        ws_git = FederatedWorkspace(id="c", kind="git_registry", endpoint="https://x")

        with (
            patch("fluid_build.forge.federation._fetch_digest_via_http", return_value="sha256:h"),
            patch(
                "fluid_build.forge.federation._fetch_digest_via_catalog", return_value="sha256:c"
            ),
            patch("fluid_build.forge.federation._fetch_digest_via_git", return_value="sha256:g"),
        ):
            assert fetch_federated_digest(ws_http, "p", "1", workspace_root=tmp_path) == "sha256:h"
            assert fetch_federated_digest(ws_cat, "p", "1", workspace_root=tmp_path) == "sha256:c"
            assert fetch_federated_digest(ws_git, "p", "1", workspace_root=tmp_path) == "sha256:g"
