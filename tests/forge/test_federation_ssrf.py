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

"""Network/SSRF hardening for the cross-mesh federation fetchers.

Covers the security-borrow-hardening pass on
``fluid_build/forge/federation.py``:

* The HTTP / catalog backends run the endpoint host through the
  canonical ``_hostname_is_private`` SSRF gate before any request and
  refuse private / link-local / loopback / cloud-metadata destinations.
* The ``FLUID_FEDERATION_HOST_ALLOWLIST`` env var opts a genuinely
  internal endpoint back in.
* Redirects are NOT followed blindly — ``httpx`` is driven with
  ``follow_redirects=False`` and a bounded manual loop re-checks the
  host gate on each hop.
* ``workspace.id`` is constrained to a path-safe slug.
* The gitpython ``GitCommandError`` branch never echoes the
  token-bearing clone URL.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx

from fluid_build.forge.federation import (
    FederatedWorkspace,
    FederationSsrfError,
    _fetch_digest_via_catalog,
    _fetch_digest_via_http,
    _guard_federation_url,
)

# ──────────────────── the SSRF host gate ───────────────────────────────


class TestGuardFederationUrl:
    def test_metadata_ip_is_rejected(self):
        """169.254.169.254 — the AWS/GCP IMDS address — is link-local
        and must be refused."""
        with pytest.raises(FederationSsrfError, match="private/loopback"):
            _guard_federation_url("http://169.254.169.254/digest", workspace_id="ws")

    def test_loopback_is_rejected(self):
        with pytest.raises(FederationSsrfError):
            _guard_federation_url("http://127.0.0.1:8080/x", workspace_id="ws")

    def test_rfc1918_host_is_rejected(self):
        # _hostname_is_private resolves; stub it to "private" so the
        # test is hermetic (no real DNS for 10.x literals needed, but
        # an IP literal resolves to itself anyway).
        with pytest.raises(FederationSsrfError):
            _guard_federation_url("http://10.1.2.3/x", workspace_id="ws")

    def test_public_host_passes(self):
        with patch("fluid_build.forge.federation._hostname_is_private", return_value=False):
            out = _guard_federation_url("https://registry.example/api", workspace_id="ws")
        assert out == "https://registry.example/api"

    def test_allowlist_overrides_private_check(self, monkeypatch):
        """An operator with a genuinely-internal endpoint opts back in
        via FLUID_FEDERATION_HOST_ALLOWLIST."""
        monkeypatch.setenv("FLUID_FEDERATION_HOST_ALLOWLIST", "internal.corp")
        # internal.corp resolves private (stubbed), but the allow-list
        # entry trumps the IP check.
        with patch("fluid_build.forge.federation._hostname_is_private", return_value=True):
            out = _guard_federation_url("https://registry.internal.corp/api", workspace_id="ws")
        assert out.startswith("https://registry.internal.corp")

    def test_allowlist_miss_still_rejects(self, monkeypatch):
        monkeypatch.setenv("FLUID_FEDERATION_HOST_ALLOWLIST", "other.corp")
        with patch("fluid_build.forge.federation._hostname_is_private", return_value=True):
            with pytest.raises(FederationSsrfError):
                _guard_federation_url("https://registry.evil.corp/api", workspace_id="ws")

    def test_no_host_is_rejected(self):
        with pytest.raises(FederationSsrfError, match="no resolvable host"):
            _guard_federation_url("not-a-url", workspace_id="ws")


# ──────────────────── fetcher-level SSRF refusal ────────────────────────


class TestFetcherRefusesPrivateEndpoint:
    @respx.mock
    def test_http_backend_blocks_metadata_endpoint(self):
        """The HTTP backend must not even issue the request when the
        endpoint points at the metadata service."""
        ws = FederatedWorkspace(id="evil", kind="http_registry", endpoint="http://169.254.169.254")
        route = respx.get(url__regex=r".*").mock(
            return_value=httpx.Response(200, text="sha256:leaked")
        )
        result = _fetch_digest_via_http(ws, "p", "1")
        assert result is None
        assert not route.called, "request must NOT leave the process"

    @respx.mock
    def test_catalog_backend_blocks_loopback(self):
        ws = FederatedWorkspace(id="lo", kind="catalog", endpoint="http://127.0.0.1:9000")
        route = respx.get(url__regex=r".*").mock(
            return_value=httpx.Response(200, json={"digest": "sha256:x"})
        )
        result = _fetch_digest_via_catalog(ws, "p", "1")
        assert result is None
        assert not route.called


# ──────────────────── redirect handling ────────────────────────────────


class TestRedirectHardening:
    @respx.mock
    def test_redirect_to_metadata_is_blocked_per_hop(self):
        """A public endpoint that 302-bounces to the metadata service
        must be caught: the per-hop host gate re-checks the Location."""
        ws = FederatedWorkspace(
            id="ext", kind="http_registry", endpoint="https://registry.example/api"
        )
        # First hop is public; it redirects to the metadata IP.
        respx.get("https://registry.example/api/p/1/digest").mock(
            return_value=httpx.Response(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
            )
        )
        metadata_route = respx.get("http://169.254.169.254/latest/meta-data/").mock(
            return_value=httpx.Response(200, text="sha256:leaked")
        )
        # First-hop host is public; the redirect target is not.
        with patch(
            "fluid_build.forge.federation._hostname_is_private",
            side_effect=lambda h: h != "registry.example",
        ):
            result = _fetch_digest_via_http(ws, "p", "1")
        assert result is None
        assert not metadata_route.called, "redirect to metadata must NOT be followed"

    @respx.mock
    def test_bounded_redirect_to_public_is_followed(self):
        """A small number of redirects between public hosts is allowed
        — the bounded loop follows up to the cap."""
        ws = FederatedWorkspace(
            id="ext", kind="http_registry", endpoint="https://registry.example/api"
        )
        respx.get("https://registry.example/api/p/1/digest").mock(
            return_value=httpx.Response(301, headers={"Location": "https://registry.example/final"})
        )
        respx.get("https://registry.example/final").mock(
            return_value=httpx.Response(200, text="sha256:followed")
        )
        with patch("fluid_build.forge.federation._hostname_is_private", return_value=False):
            result = _fetch_digest_via_http(ws, "p", "1")
        assert result == "sha256:followed"

    @respx.mock
    def test_redirect_loop_is_bounded(self):
        """An endless redirect chain terminates at the cap and yields
        None rather than looping forever."""
        ws = FederatedWorkspace(
            id="ext", kind="http_registry", endpoint="https://registry.example/api"
        )
        respx.get("https://registry.example/api/p/1/digest").mock(
            return_value=httpx.Response(
                302, headers={"Location": "https://registry.example/api/p/1/digest"}
            )
        )
        with patch("fluid_build.forge.federation._hostname_is_private", return_value=False):
            result = _fetch_digest_via_http(ws, "p", "1")
        assert result is None


# ──────────────────── workspace.id path-component validation ────────────


class TestWorkspaceIdValidation:
    def test_traversal_id_is_rejected(self):
        with pytest.raises(ValueError, match="invalid"):
            FederatedWorkspace.from_dict(
                {"id": "../../etc", "kind": "http_registry", "endpoint": "https://x.example"}
            )

    def test_absolute_path_id_is_rejected(self):
        with pytest.raises(ValueError, match="invalid"):
            FederatedWorkspace.from_dict(
                {"id": "/etc/passwd", "kind": "http_registry", "endpoint": "https://x.example"}
            )

    def test_slash_in_id_is_rejected(self):
        with pytest.raises(ValueError, match="invalid"):
            FederatedWorkspace.from_dict(
                {"id": "a/b", "kind": "http_registry", "endpoint": "https://x.example"}
            )

    def test_overlong_id_is_rejected(self):
        with pytest.raises(ValueError, match="invalid"):
            FederatedWorkspace.from_dict(
                {"id": "x" * 65, "kind": "http_registry", "endpoint": "https://x.example"}
            )

    def test_normal_id_is_accepted(self):
        ws = FederatedWorkspace.from_dict(
            {"id": "telco-billing.v2_1", "kind": "http_registry", "endpoint": "https://x.example"}
        )
        assert ws.id == "telco-billing.v2_1"


# ──────────────────── gitpython auth-token leak ─────────────────────────


class TestGitpythonTokenLeak:
    def test_gitcommanderror_message_not_logged(self, tmp_path, monkeypatch, caplog):
        """A failed gitpython clone must log only the exception class —
        never the GitCommandError body, which echoes the
        ``x-access-token:<TOKEN>@host`` clone URL."""
        from fluid_build.forge import federation as _fed

        git_mod = pytest.importorskip("git")

        ws = FederatedWorkspace(
            id="ext",
            kind="git_registry",
            endpoint="https://github.example/repo",
            auth_mode="github_token",
            auth_secret_ref="GH_TOKEN",
        )
        monkeypatch.setenv("GH_TOKEN", "ghp_SUPERSECRETTOKEN")
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(tmp_path / "home_cache") if "~" in p else p,
        )

        # GitCommandError stringifies to a command line that embeds the
        # token-bearing URL — simulate that exact shape.
        leaky = git_mod.GitCommandError(
            ["git", "clone", "https://x-access-token:ghp_SUPERSECRETTOKEN@github.example/repo"],
            128,
            b"fatal: Authentication failed",
        )

        # ``_git_clone_or_pull_via_gitpython`` does ``from git import
        # Repo`` locally, so patch the attribute on the ``git`` module.
        with patch.object(git_mod, "Repo") as mock_repo:
            mock_repo.clone_from.side_effect = leaky
            with caplog.at_level("WARNING"):
                result = _fed._git_clone_or_pull_via_gitpython(
                    auth_url="https://x-access-token:ghp_SUPERSECRETTOKEN@github.example/repo",
                    cache_dir=tmp_path / "clone",
                    workspace_id="ext",
                )

        assert result is False
        all_log_text = " ".join(r.getMessage() for r in caplog.records)
        assert "ghp_SUPERSECRETTOKEN" not in all_log_text
        assert "x-access-token" not in all_log_text
        # The class name + a static refusal message is what we DO log.
        assert "GitCommandError" in all_log_text

    def test_shellout_clone_failure_does_not_log_token(self, tmp_path, caplog):
        """The shell-out fallback's clone-failed branch must log only the
        exception class — ``CalledProcessError.__str__`` echoes the full
        git argv, which carries the token-bearing clone URL."""
        import subprocess

        from fluid_build.forge import federation as _fed

        auth_url = "https://x-access-token:ghp_LEAKYTOKEN@github.example/repo"
        leaky = subprocess.CalledProcessError(
            128,
            ["git", "clone", "--depth", "1", "--", auth_url, str(tmp_path / "clone")],
            output=b"",
            stderr=b"fatal: Authentication failed",
        )

        with patch("subprocess.run", side_effect=leaky):
            with caplog.at_level("WARNING"):
                ok = _fed._git_clone_or_pull_via_shellout(
                    auth_url=auth_url,
                    cache_dir=tmp_path / "clone",
                    workspace_id="ext",
                )

        assert ok is False
        all_log_text = " ".join(r.getMessage() for r in caplog.records)
        assert "ghp_LEAKYTOKEN" not in all_log_text
        assert "x-access-token" not in all_log_text
        assert "CalledProcessError" in all_log_text
