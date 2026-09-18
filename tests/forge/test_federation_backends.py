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

import hashlib
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from fluid_build.forge.core.plan_digest import compute_contract_digest
from fluid_build.forge.federation import (
    FederatedWorkspace,
    _fetch_digest_via_catalog,
    _fetch_digest_via_git,
    _fetch_digest_via_http,
    fetch_federated_digest,
    store_cached_digest,
)
from fluid_build.util.safe_yaml import load_yaml_safe

# The federation HTTP/catalog backends were migrated off
# ``urllib.request.urlopen`` (which followed up to 10 redirects and
# re-sent auth headers cross-host) to ``httpx`` with
# ``follow_redirects=False`` + an SSRF host gate. These tests therefore
# mock ``httpx`` via ``respx`` and stub the ``_hostname_is_private``
# gate to "public host" so the synthetic test endpoints aren't refused
# by the fail-closed DNS check. The SSRF gate itself has dedicated
# coverage in ``test_federation_ssrf.py``.

_PUBLIC = "fluid_build.forge.federation._hostname_is_private"


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

    @respx.mock
    def test_happy_path_returns_digest(self):
        ws = self._ws()
        respx.get("https://registry.example/api/orders_v1/1/digest").mock(
            return_value=httpx.Response(200, text="sha256:abc123\n")
        )
        with patch(_PUBLIC, return_value=False):
            result = _fetch_digest_via_http(ws, "orders_v1", "1")
        assert result == "sha256:abc123"

    @respx.mock
    def test_bearer_auth_header_built_from_secret_ref(self, monkeypatch):
        ws = self._ws(auth_mode="bearer", auth_secret_ref="REGISTRY_TOKEN")
        monkeypatch.setenv("REGISTRY_TOKEN", "tok-9876")

        route = respx.get("https://registry.example/api/p/1/digest").mock(
            return_value=httpx.Response(200, text="sha256:def\n")
        )
        with patch(_PUBLIC, return_value=False):
            result = _fetch_digest_via_http(ws, "p", "1")
        assert result == "sha256:def"
        assert route.calls[0].request.headers.get("Authorization") == "Bearer tok-9876"

    @respx.mock
    def test_http_error_returns_none(self):
        ws = self._ws()
        respx.get("https://registry.example/api/p/1/digest").mock(
            return_value=httpx.Response(403, text="forbidden")
        )
        with patch(_PUBLIC, return_value=False):
            result = _fetch_digest_via_http(ws, "p", "1")
        assert result is None

    @respx.mock
    def test_unexpected_body_returns_none(self):
        ws = self._ws()
        respx.get("https://registry.example/api/p/1/digest").mock(
            return_value=httpx.Response(200, text="<html>not a digest</html>")
        )
        with patch(_PUBLIC, return_value=False):
            result = _fetch_digest_via_http(ws, "p", "1")
        assert result is None

    @respx.mock
    def test_redirect_is_not_followed_by_default(self):
        """``follow_redirects=False`` is the SSRF-safe default: a bare
        30x without an in-bounds Location-chase that resolves a digest
        yields ``None`` rather than chasing the redirect blindly."""
        ws = self._ws()
        respx.get("https://registry.example/api/p/1/digest").mock(
            return_value=httpx.Response(302, headers={"Location": "https://registry.example/x"})
        )
        # The redirect target also 30x-loops so the bounded loop gives up.
        respx.get("https://registry.example/x").mock(
            return_value=httpx.Response(302, headers={"Location": "https://registry.example/x"})
        )
        with patch(_PUBLIC, return_value=False):
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

    @respx.mock
    def test_happy_path_returns_digest(self):
        ws = self._ws()
        respx.get("https://catalog.example/api/products/orders/versions/1").mock(
            return_value=httpx.Response(200, json={"digest": "sha256:cat"})
        )
        with patch(_PUBLIC, return_value=False):
            result = _fetch_digest_via_catalog(ws, "orders", "1")
        assert result == "sha256:cat"

    @respx.mock
    def test_missing_digest_field_returns_none(self):
        ws = self._ws()
        respx.get("https://catalog.example/api/products/orders/versions/1").mock(
            return_value=httpx.Response(200, json={"name": "orders"})
        )
        with patch(_PUBLIC, return_value=False):
            result = _fetch_digest_via_catalog(ws, "orders", "1")
        assert result is None

    @respx.mock
    def test_404_returns_none(self):
        ws = self._ws()
        respx.get("https://catalog.example/api/products/orders/versions/1").mock(
            return_value=httpx.Response(404, text="nf")
        )
        with patch(_PUBLIC, return_value=False):
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
        """The digest MUST be the canonical contract digest.

        Asserting only ``startswith("sha256:")`` is not enough — the
        raw-text fallback this file previously tolerated satisfied that
        too. Pin the exact value against
        :func:`compute_contract_digest` of the parsed contract.
        """
        ws = self._ws()
        contract_text = "fluidVersion: 0.7.3\nid: external.orders\nexposes:\n  - id: orders\n"
        with patch(
            "fluid_build.forge.federation._git_read_contract",
            return_value=contract_text,
        ):
            result = _fetch_digest_via_git(ws, "external.orders", "1")
        assert result == compute_contract_digest(load_yaml_safe(contract_text))

    def test_missing_repo_returns_none(self):
        ws = self._ws()
        with patch("fluid_build.forge.federation._git_read_contract", return_value=None):
            result = _fetch_digest_via_git(ws, "external.orders", "1")
        assert result is None

    def test_git_fetch_always_goes_through_the_bounded_shellout(self, tmp_path: Path, monkeypatch):
        """There is exactly one git path, and it is the one that can be
        given a timeout.

        This replaces three tests that pinned a gitpython-first dispatch
        order. That path was removed because it cannot be bounded, and
        ``fluid apply`` depends on this call returning. Measured against a
        TCP listener that accepts and never speaks, GitPython 3.1.62:
        ``Repo.clone_from(..., kill_after_timeout=3)`` was still running at
        90s, while ``subprocess.run([...], timeout=3)`` raised
        TimeoutExpired at 3.0s. ``kill_after_timeout`` is *accepted* by
        clone_from (it sits in GitPython's execute_kwargs, so it is not
        rejected the way an unknown kwarg is) but it does not kill the
        clone -- which is worse than offering no timeout at all, because
        ``fluid doctor`` then advertises a cap that does not exist.

        Nothing was lost: GitPython shells out to the same ``git`` binary,
        so it was never a fallback for git being missing.
        """
        ws = FederatedWorkspace(id="ext-sh", kind="git_registry", endpoint="https://example.com/r")
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(tmp_path / "home_cache") if "~" in p else p,
        )

        with (
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

        assert mock_sh.called, "the git backend must use the bounded shell-out path"
        assert result is not None and result.startswith("sha256:")

    def test_no_gitpython_network_call_is_reintroduced(self):
        """Guard against the unbounded path coming back.

        Any ``Repo.clone_from`` / ``origin.fetch`` in this module is a
        network call GitPython gives no working way to bound, so it must
        not reappear -- with or without a ``kill_after_timeout`` that does
        nothing.
        """
        import ast
        from pathlib import Path as _P

        src = (
            _P(__file__).resolve().parents[2] / "fluid_build" / "forge" / "federation.py"
        ).read_text(encoding="utf-8")
        offenders = [
            node.lineno
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"clone_from", "fetch"}
        ]
        assert not offenders, (
            f"gitpython network call(s) reintroduced at line(s) {offenders} -- "
            "these cannot be bounded; use _git_clone_or_pull_via_shellout"
        )

    def test_shellout_failure_aborts_without_a_digest(self, tmp_path: Path, monkeypatch):
        """A failed clone must yield no digest -- federation fails closed,
        never on a weaker or stale answer."""
        ws = FederatedWorkspace(
            id="ext-sh-fail", kind="git_registry", endpoint="https://example.com/r"
        )
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(tmp_path / "home_cache") if "~" in p else p,
        )

        with patch(
            "fluid_build.forge.federation._git_clone_or_pull_via_shellout",
            return_value=False,
        ):
            assert _fetch_digest_via_git(ws, "ext.x", "1") is None


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


# ──────────── Git backend: canonical-digest regression ─────────────────


class TestGitBackendDigestIsCanonicalNotRawText:
    """Regression pins for the git backend's digest algorithm.

    ``_fetch_digest_via_git`` used to import a ``compute_contract_digest``
    that did not exist anywhere in ``fluid_build``. The import was wrapped
    in ``except Exception``, so it raised ImportError on *every* call and
    silently fell through to ``sha256(contract_text)`` — a raw-text hash —
    while the docstring and the call site both advertised the canonical
    contract digest.

    That divergence is invisible in a single workspace and only bites
    across peers: two meshes holding the byte-for-byte *same meaning*
    but different YAML formatting compute different digests, so
    ``upstreamDigest`` pinning reports permanent phantom drift.

    These tests fail if the raw-text branch (or any other
    formatting-sensitive hash) ever comes back.
    """

    def _ws(self) -> FederatedWorkspace:
        return FederatedWorkspace(
            id="ext", kind="git_registry", endpoint="https://github.example/foo"
        )

    # The same contract, formatted three ways a real peer might write it:
    # block vs flow style, quoted vs bare scalars, reordered keys,
    # comments, and CRLF line endings.
    _CANONICAL = (
        "fluidVersion: 0.7.3\n"
        "id: external.orders\n"
        "exposes:\n"
        "  - id: orders\n"
        "    fields: [order_id, customer_id]\n"
    )
    _REFORMATTED = (
        "# upstream peer formats its YAML differently\n"
        "exposes:\n"
        "- fields: ['order_id', \"customer_id\"]\n"
        '  id: "orders"\n'
        "id: 'external.orders'\n"
        "fluidVersion: 0.7.3\n"
    )
    _CRLF = _CANONICAL.replace("\n", "\r\n")

    def _digest_for(self, contract_text: str):
        with patch(
            "fluid_build.forge.federation._git_read_contract",
            return_value=contract_text,
        ):
            return _fetch_digest_via_git(self._ws(), "external.orders", "1")

    def test_compute_contract_digest_actually_exists(self):
        """The symbol the git backend imports must be real.

        This is the root-cause pin: the original bug was an import of a
        name that was never defined, so ``fluid_build.forge.federation``
        must expose the *same object* ``plan_digest`` defines.
        """
        from fluid_build.forge import federation as _fed

        assert _fed.compute_contract_digest is compute_contract_digest

    def test_digest_is_not_the_raw_text_hash(self):
        """The old fallback hashed ``contract_text`` directly. Assert the
        returned digest is NOT that value."""
        raw_text_digest = "sha256:" + hashlib.sha256(self._CANONICAL.encode("utf-8")).hexdigest()
        result = self._digest_for(self._CANONICAL)
        assert result is not None
        assert result != raw_text_digest, (
            "git backend fell through to the raw-text hash — the canonical "
            "contract digest is not being used"
        )
        assert result == compute_contract_digest(load_yaml_safe(self._CANONICAL))

    def test_peers_formatting_the_same_contract_differently_agree(self):
        """The invariant the canonical digest exists to provide."""
        canonical = self._digest_for(self._CANONICAL)
        reformatted = self._digest_for(self._REFORMATTED)
        crlf = self._digest_for(self._CRLF)

        assert canonical is not None
        assert canonical == reformatted, (
            "two peers holding the semantically identical contract must "
            "agree on the digest regardless of YAML formatting"
        )
        assert canonical == crlf, "CRLF line endings must not perturb the digest"

        # Sanity: a raw-text hash genuinely WOULD have disagreed, so the
        # assertions above are not vacuous.
        assert (
            hashlib.sha256(self._CANONICAL.encode()).hexdigest()
            != hashlib.sha256(self._REFORMATTED.encode()).hexdigest()
        )

    def test_real_content_change_still_changes_the_digest(self):
        """Format-insensitivity must not become content-insensitivity."""
        changed = self._CANONICAL.replace("customer_id", "customer_key")
        assert self._digest_for(self._CANONICAL) != self._digest_for(changed)

    def test_no_import_guard_swallows_a_missing_helper(self):
        """A broken ``compute_contract_digest`` must fail loudly, never
        degrade to a weaker digest.

        Simulating the original failure mode: if the helper raises, the
        error propagates instead of being traded for a raw-text hash.
        """
        with (
            patch(
                "fluid_build.forge.federation._git_read_contract",
                return_value=self._CANONICAL,
            ),
            patch(
                "fluid_build.forge.federation.compute_contract_digest",
                side_effect=ImportError("boom"),
            ),
            pytest.raises(ImportError),
        ):
            _fetch_digest_via_git(self._ws(), "external.orders", "1")

    @pytest.mark.parametrize(
        "bad_text",
        [
            "fluidVersion: [unclosed\n",  # unparseable YAML
            "just a bare scalar\n",  # parses, but not a mapping
            "- a\n- b\n",  # parses to a list, not a mapping
            "# only a comment\n",  # parses to None
        ],
    )
    def test_malformed_upstream_fails_closed(self, bad_text: str):
        """A malformed upstream contract yields ``None`` (escalated to a
        violation by the caller) — never a fallback digest."""
        assert self._digest_for(bad_text) is None
