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

"""How federation handles the credential named by ``auth.secret_ref``.

Four properties, each pinned here:

1. **git** -- the token is handed to git per process as an
   ``http.<endpoint>.extraHeader`` entry appended to ``GIT_CONFIG_PARAMETERS``.
   It is never in the clone URL, never in argv and never in ``.git/config``;
   caches written by older versions are repaired on the next refresh.
2. **Which variable may go where** -- ``GITHUB_TOKEN`` / ``GH_TOKEN`` are
   allowed by default but only toward GitHub hosts. Anything else must be
   listed in ``FLUID_FEDERATION_SECRET_REF_ALLOWLIST`` (``NAME`` or the
   recommended ``NAME@host``). Outside that policy the fetch fails with
   :class:`FederationCredentialPolicyError` and nothing is sent.
3. **Transport** -- a credential only travels over https (or ssh for
   git, which never carries the token). http/git endpoints still work
   anonymously.
4. **Redirects** -- over HTTP, a hop to another origin drops
   ``Authorization`` and the other credential headers, and an https -> http
   downgrade is refused. git with a credential does not follow redirects
   (real-git proof in ``test_federation_git_redirects.py``).
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, List
from unittest.mock import patch

import httpx
import pytest
import respx

from fluid_build.forge import federation
from fluid_build.forge.federation import (
    FederatedWorkspace,
    FederationManifest,
    _fetch_digest_via_catalog,
    _fetch_digest_via_http,
    validate_federated_consumes,
)
from tests.forge.test_federation_resilience import (  # noqa: F401 - pytest fixtures
    _git,
    local_cache,
    upstream_repo,
)

pytestmark = pytest.mark.unit

_PUBLIC = "fluid_build.forge.federation._hostname_is_private"
_ALLOWLIST_ENV = "FLUID_FEDERATION_SECRET_REF_ALLOWLIST"
#: Test value only. Long and distinctive so a substring check is meaningful.
_TOKEN = "fedtest-0000-not-a-real-credential-7f3a9c"
_POLICY_ERROR = "FederationCredentialPolicyError"
#: Names of environment variables, not values. Held in constants because the
#: secret scanner reads ``auth_secret_ref="<literal>"`` as a credential.
_GITHUB_VAR = "GITHUB_TOKEN"
_REGISTRY_VAR = "REGISTRY_TOKEN"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        _ALLOWLIST_ENV,
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "REGISTRY_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_PARAMETERS",
        "FLUID_FEDERATION_HOST_ALLOWLIST",
    ):
        monkeypatch.delenv(name, raising=False)


def _call(fn, *args) -> tuple[Any, BaseException | None]:
    try:
        return fn(*args), None
    except Exception as exc:  # noqa: BLE001 - the test inspects the type
        return None, exc


def _http_ws(
    endpoint: str, *, mode: str = "bearer", ref: str = "GITHUB_TOKEN", kind: str = "http_registry"
) -> FederatedWorkspace:
    return FederatedWorkspace(
        id="ext", kind=kind, endpoint=endpoint, auth_mode=mode, auth_secret_ref=ref
    )


# ──────────────────── 1. git: token never in URL / argv / config ─────────


class _RecordingRun:
    """Stand-in for ``subprocess.run`` that records argv + env."""

    def __init__(self) -> None:
        self.calls: List[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "env": kwargs.get("env")})
        if len(argv) > 1 and argv[1] == "clone":
            Path(argv[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, 0, b"", b"")


class TestGitTokenStaysOutOfArgvAndConfig:
    def _ws(self, endpoint: str = "https://github.com/acme/mesh") -> FederatedWorkspace:
        return _http_ws(endpoint, mode="github_token", kind="git_registry")

    def test_clone_argv_carries_no_token_and_env_carries_the_header(
        self, local_cache: Path, monkeypatch
    ):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
        rec = _RecordingRun()
        with patch("subprocess.run", rec):
            federation._git_read_contract(self._ws(), "acme.orders")

        clone = [c for c in rec.calls if "clone" in c["argv"]]
        assert clone, "a clone must run on a cold cache"
        for call in rec.calls:
            assert not any(_TOKEN in part for part in call["argv"]), call["argv"]
            assert not any("x-access-token" in part for part in call["argv"]), call["argv"]
        assert "https://github.com/acme/mesh" in clone[0]["argv"]

        env = clone[0]["env"] or {}
        params = env["GIT_CONFIG_PARAMETERS"]
        b64 = base64.b64encode(f"x-access-token:{_TOKEN}".encode()).decode()
        # Keyed to the endpoint URL, at the two scopes git ranks highest for it.
        for scope in ("https://github.com/acme/mesh", "https://github.com/acme/mesh//"):
            # An empty value first resets any extraHeader configured before.
            reset = f"'http.{scope}.extraHeader'=''"
            assert f"{reset} 'http.{scope}.extraHeader'='Authorization: Basic {b64}'" in params
            # git would otherwise rebase onto a redirect target and send the
            # header there; a redirect fails the authenticated fetch instead.
            assert f"'http.{scope}.followRedirects'='false'" in params
        assert "GIT_CONFIG_COUNT" not in env
        assert env.get("GIT_TERMINAL_PROMPT") == "0"

    def test_refresh_fetch_gets_the_header_and_rewrites_the_remote(
        self, local_cache: Path, monkeypatch
    ):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
        (local_cache / "ext" / ".git").mkdir(parents=True)
        rec = _RecordingRun()
        with patch("subprocess.run", rec):
            federation._git_read_contract(self._ws(), "acme.orders")

        argvs = [c["argv"] for c in rec.calls]
        set_url = [a for a in argvs if "set-url" in a]
        assert set_url and set_url[0][-1] == "https://github.com/acme/mesh"
        fetch = [c for c in rec.calls if "fetch" in c["argv"]]
        assert fetch and "extraHeader" in (fetch[0]["env"] or {}).get("GIT_CONFIG_PARAMETERS", "")

    def test_inherited_git_config_is_kept_and_ours_comes_after_it(
        self, local_cache: Path, monkeypatch
    ):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.askPass")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "")
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.longpaths'='true'")
        rec = _RecordingRun()
        with patch("subprocess.run", rec):
            federation._git_read_contract(self._ws(), "acme.orders")
        # The clone's environment: `git config` reads the operator's settings first.
        env = next(c for c in rec.calls if "clone" in c["argv"])["env"]
        # The pairs are untouched: git reads them before GIT_CONFIG_PARAMETERS.
        assert (env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_KEY_0"]) == ("1", "core.askPass")
        assert "GIT_CONFIG_KEY_1" not in env
        # Appended after the inherited value, the way `git -c` adds an entry.
        assert env["GIT_CONFIG_PARAMETERS"].startswith("'core.longpaths'='true' 'http.")

    def test_ssh_endpoint_gets_no_token(self, local_cache: Path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
        rec = _RecordingRun()
        with patch("subprocess.run", rec):
            federation._git_read_contract(self._ws("ssh://git@github.com/acme/mesh"), "acme.orders")
        for call in rec.calls:
            assert _TOKEN not in repr(call)
            assert "GIT_CONFIG_PARAMETERS" not in (call["env"] or {})

    @pytest.mark.parametrize("endpoint", ["http://github.com/acme/mesh", "git://github.com/acme"])
    def test_cleartext_git_endpoint_with_a_credential_is_refused(
        self, endpoint: str, local_cache: Path, monkeypatch
    ):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
        rec = _RecordingRun()
        with patch("subprocess.run", rec):
            _, err = _call(federation._git_read_contract, self._ws(endpoint), "acme.orders")
        assert not rec.calls, "nothing may run when the credential would travel in cleartext"
        assert type(err).__name__ == _POLICY_ERROR
        assert "https" in str(err)
        assert _TOKEN not in str(err)

    def test_cleartext_endpoint_is_refused_for_the_scheme_before_the_host(
        self, local_cache: Path, monkeypatch
    ):
        """No allow-list entry can make a cleartext endpoint carry a credential,
        so the refusal must say so instead of suggesting one."""
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
        rec = _RecordingRun()
        with patch("subprocess.run", rec):
            _, err = _call(
                federation._git_read_contract, self._ws("git://git.example/acme"), "acme.orders"
            )
        assert not rec.calls
        assert type(err).__name__ == _POLICY_ERROR
        assert "cleartext" in str(err)
        assert _ALLOWLIST_ENV not in str(err)

    def test_git_endpoint_with_userinfo_gets_no_header(self, local_cache: Path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
        rec = _RecordingRun()
        with patch("subprocess.run", rec):
            federation._git_read_contract(
                self._ws("https://github.com@collector.example/acme"), "acme.orders"
            )
        for call in rec.calls:
            assert _TOKEN not in repr(call)
            assert "GIT_CONFIG_PARAMETERS" not in (call["env"] or {})

    def test_git_token_bound_to_github_is_not_sent_elsewhere(self, local_cache: Path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
        rec = _RecordingRun()
        with patch("subprocess.run", rec):
            _, err = _call(
                federation._git_read_contract,
                self._ws("https://git.example.org/acme/mesh"),
                "acme.orders",
            )
        assert not rec.calls
        assert type(err).__name__ == _POLICY_ERROR
        assert "GITHUB_TOKEN" in str(err) and _ALLOWLIST_ENV in str(err)

    def test_the_federation_doc_names_the_settings_git_is_handed(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
        endpoint = "https://github.com/acme/mesh"
        env = federation._git_auth_env(self._ws(endpoint))
        assert set(env) == {"GIT_CONFIG_PARAMETERS", "GIT_TERMINAL_PROMPT"}
        assert f"'http.{endpoint}.extraHeader'=''" in env["GIT_CONFIG_PARAMETERS"]
        assert f"'http.{endpoint}.followRedirects'='false'" in env["GIT_CONFIG_PARAMETERS"]
        root = Path(federation.__file__).resolve().parents[2]
        doc = re.sub(r"\s+", " ", (root / "docs" / "FEDERATION.md").read_text(encoding="utf-8"))
        for named in (
            "`GIT_CONFIG_PARAMETERS`",
            "`http.<endpoint>.extraHeader`",
            "`http.<endpoint>.followRedirects=false`",
            "empty `extraHeader`",
            # The reset is followed by the operator's other headers.
            "then hands back the extra headers your git configuration sets for the "
            "endpoint, except `Authorization`",
            "Your other extra headers for the endpoint are still sent.",
            # A rewrite of the endpoint is compared with the endpoint itself.
            "another scheme, host, port or path, including a path below the endpoint's",
            "A rewrite that only adds a user name or other userinfo",
        ):
            assert named in doc, named
        assert "are not sent on an authenticated fetch" not in doc


@pytest.mark.skipif(shutil.which("git") is None, reason="git binary not installed")
class TestRealGitBinary:
    """Real git, so the env-injected config is proven well-formed and the
    on-disk state is inspected rather than assumed."""

    def test_clone_with_credential_env_leaves_no_token_on_disk(
        self, upstream_repo: Path, local_cache: Path, monkeypatch
    ):
        monkeypatch.setenv("REGISTRY_TOKEN", _TOKEN)
        monkeypatch.setenv(_ALLOWLIST_ENV, "REGISTRY_TOKEN@git.example.invalid")
        ws = FederatedWorkspace(
            id="real",
            kind="git_registry",
            endpoint="https://git.example.invalid/acme/mesh",
            auth_mode="github_token",
            auth_secret_ref=_REGISTRY_VAR,
        )
        # The endpoint allow-list refuses file:// on purpose; point the
        # remote at the local fixture through the remote-URL seam only.
        with patch.object(
            federation, "_git_remote_url", return_value=str(upstream_repo), create=True
        ):
            text = federation._git_read_contract(ws, "telco.orders")
        assert text == (upstream_repo / "telco.orders" / "contract.fluid.yaml").read_text()
        config = (local_cache / "real" / ".git" / "config").read_text()
        assert _TOKEN not in config
        assert "x-access-token" not in config

    def test_git_reads_the_entries_after_inherited_config_and_unquotes_them(
        self, tmp_path: Path, monkeypatch
    ):
        """git itself parses the value: inherited entries are kept and read
        first, and ours arrive intact, quote in the endpoint included."""
        endpoint = "https://git.example.invalid/acme/it's-mesh.git"
        monkeypatch.setenv("REGISTRY_TOKEN", _TOKEN)
        monkeypatch.setenv(_ALLOWLIST_ENV, "REGISTRY_TOKEN@git.example.invalid")
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.askPass")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "")
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.longpaths'='true'")
        ws = FederatedWorkspace(
            id="quoted",
            kind="git_registry",
            endpoint=endpoint,
            auth_mode="github_token",
            auth_secret_ref=_REGISTRY_VAR,
        )
        env = {**os.environ, **federation._git_auth_env(ws)}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        listed = subprocess.run(
            ["git", "config", "--list", "--show-scope"],
            env=env,
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        entries = [
            tuple(line.split("\t", 1)[1].split("=", 1))
            for line in listed.splitlines()
            if line.startswith("command\t")
        ]
        header = (
            "Authorization: Basic " + base64.b64encode(f"x-access-token:{_TOKEN}".encode()).decode()
        )
        ours = [
            entry
            for scope in (endpoint, endpoint + "//")
            for entry in (
                (f"http.{scope}.extraheader", ""),
                (f"http.{scope}.extraheader", header),
                (f"http.{scope}.followredirects", "false"),
            )
        ]
        assert entries == [("core.askpass", ""), ("core.longpaths", "true"), *ours]

    def test_refresh_repairs_a_cache_written_with_a_token_in_the_remote(
        self, tmp_path: Path, upstream_repo: Path, local_cache: Path, monkeypatch
    ):
        """A cache cloned by an older version holds the token in
        remote.origin.url; the next refresh must rewrite it."""
        cache = local_cache / "legacy"
        cache.parent.mkdir(parents=True)
        _git("clone", "-q", str(upstream_repo), str(cache), cwd=tmp_path)
        legacy = f"https://x-access-token:{_TOKEN}@git.example.invalid/acme/mesh"
        _git("remote", "set-url", "origin", legacy, cwd=cache)
        assert _TOKEN in (cache / ".git" / "config").read_text()

        monkeypatch.setenv("FLUID_FEDERATION_TIMEOUT_SECONDS", "10")
        ws = FederatedWorkspace(
            id="legacy", kind="git_registry", endpoint="https://git.example.invalid/acme/mesh"
        )
        federation._git_read_contract(ws, "telco.orders")

        config = (cache / ".git" / "config").read_text()
        assert _TOKEN not in config, "the legacy token must be scrubbed from .git/config"
        remote = subprocess.run(
            ["git", "-C", str(cache), "config", "remote.origin.url"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert remote == "https://git.example.invalid/acme/mesh"

    def test_cache_dir_that_is_not_a_clone_never_touches_an_enclosing_repo(
        self, tmp_path: Path, upstream_repo: Path, local_cache: Path, monkeypatch
    ):
        """A leftover cache dir (an interrupted clone) inside another repo,
        such as a home directory kept in git, must be replaced by a fresh
        clone. git must not walk up and act on the enclosing repo."""
        dotfiles = "https://dotfiles.example.invalid/me.git"
        local_cache.mkdir(parents=True)
        _git("init", "-q", "-b", "main", str(local_cache), cwd=tmp_path)
        _git("remote", "add", "origin", dotfiles, cwd=local_cache)
        (local_cache / "telco").mkdir()

        monkeypatch.setenv("FLUID_FEDERATION_TIMEOUT_SECONDS", "10")
        ws = FederatedWorkspace(
            id="telco", kind="git_registry", endpoint="https://git.example.invalid/telco"
        )
        with patch.object(federation, "_git_remote_url", return_value=str(upstream_repo)):
            text = federation._git_read_contract(ws, "telco.orders")

        enclosing = subprocess.run(
            ["git", "-C", str(local_cache), "config", "remote.origin.url"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert enclosing == dotfiles, "the enclosing repo's origin must be left alone"
        assert (local_cache / "telco" / ".git").is_dir(), "the cache is a fresh clone"
        assert text == (upstream_repo / "telco.orders" / "contract.fluid.yaml").read_text()

    def test_failed_refresh_is_unreachable_not_the_stale_contract(
        self, tmp_path: Path, upstream_repo: Path, local_cache: Path, monkeypatch
    ):
        """When the refresh fetch fails the cached contract is stale; it must
        not be reported as a live read of the upstream."""
        monkeypatch.setenv("FLUID_FEDERATION_TIMEOUT_SECONDS", "10")
        ws = FederatedWorkspace(
            id="stale", kind="git_registry", endpoint="https://git.example.invalid/telco"
        )
        with patch.object(federation, "_git_remote_url", return_value=str(upstream_repo)):
            assert federation._git_read_contract(ws, "telco.orders")

        gone = str(tmp_path / "no-such-upstream.git")
        with patch.object(federation, "_git_remote_url", return_value=gone):
            assert federation._git_read_contract(ws, "telco.orders") is None

    def test_refresh_picks_up_a_new_upstream_commit(
        self, upstream_repo: Path, local_cache: Path, monkeypatch
    ):
        monkeypatch.setenv("FLUID_FEDERATION_TIMEOUT_SECONDS", "10")
        ws = FederatedWorkspace(
            id="fresh", kind="git_registry", endpoint="https://git.example.invalid/telco"
        )
        contract = upstream_repo / "telco.orders" / "contract.fluid.yaml"
        with patch.object(federation, "_git_remote_url", return_value=str(upstream_repo)):
            assert federation._git_read_contract(ws, "telco.orders") == contract.read_text()
            contract.write_text(contract.read_text() + "# v2\n", encoding="utf-8")
            _git("commit", "-qam", "v2", cwd=upstream_repo)
            assert federation._git_read_contract(ws, "telco.orders") == contract.read_text()


# ──────────────────── 2. which variable may go where ────────────────────


class TestSecretRefPolicy:
    @respx.mock
    def test_github_token_is_sent_to_a_github_host(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
        route = respx.get("https://api.github.com/p/1/digest").mock(
            return_value=httpx.Response(200, text="sha256:ok")
        )
        with patch(_PUBLIC, return_value=False):
            assert _fetch_digest_via_http(_http_ws("https://api.github.com"), "p", "1") == (
                "sha256:ok"
            )
        assert route.calls[0].request.headers["Authorization"] == f"Bearer {_TOKEN}"

    @respx.mock
    @pytest.mark.parametrize("ref", ["GITHUB_TOKEN", "GH_TOKEN"])
    def test_github_token_is_not_sent_to_another_host(self, ref: str, monkeypatch):
        monkeypatch.setenv(ref, _TOKEN)
        route = respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="sha256:x"))
        with patch(_PUBLIC, return_value=False):
            _, err = _call(
                _fetch_digest_via_http, _http_ws("https://collector.example", ref=ref), "p", "1"
            )
        assert not route.called, "the credential must never leave the process"
        assert type(err).__name__ == _POLICY_ERROR
        assert ref in str(err) and _ALLOWLIST_ENV in str(err)
        assert _TOKEN not in str(err)

    @respx.mock
    def test_unlisted_variable_is_refused_even_when_unset(self, monkeypatch):
        route = respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="sha256:x"))
        with patch(_PUBLIC, return_value=False):
            _, err = _call(
                _fetch_digest_via_http,
                _http_ws("https://registry.example", ref="AWS_SECRET_ACCESS_KEY"),
                "p",
                "1",
            )
        assert not route.called
        assert type(err).__name__ == _POLICY_ERROR
        assert "AWS_SECRET_ACCESS_KEY" in str(err)

    @respx.mock
    @pytest.mark.parametrize(
        "allowlist",
        [
            "REGISTRY_TOKEN@registry.example",
            "OTHER, REGISTRY_TOKEN@REGISTRY.example ",
            "REGISTRY_TOKEN",
        ],
    )
    def test_allowlisted_variable_is_sent(self, allowlist: str, monkeypatch):
        monkeypatch.setenv("REGISTRY_TOKEN", _TOKEN)
        monkeypatch.setenv(_ALLOWLIST_ENV, allowlist)
        route = respx.get("https://registry.example/p/1/digest").mock(
            return_value=httpx.Response(200, text="sha256:ok")
        )
        ws = _http_ws("https://registry.example", ref="REGISTRY_TOKEN")
        with patch(_PUBLIC, return_value=False):
            assert _fetch_digest_via_http(ws, "p", "1") == "sha256:ok"
        assert route.calls[0].request.headers["Authorization"] == f"Bearer {_TOKEN}"

    @respx.mock
    def test_host_bound_entry_does_not_cover_another_host(self, monkeypatch):
        monkeypatch.setenv("REGISTRY_TOKEN", _TOKEN)
        monkeypatch.setenv(_ALLOWLIST_ENV, "REGISTRY_TOKEN@registry.example")
        route = respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="sha256:x"))
        ws = _http_ws("https://registry.example.attacker.test", ref="REGISTRY_TOKEN")
        with patch(_PUBLIC, return_value=False):
            _, err = _call(_fetch_digest_via_http, ws, "p", "1")
        assert not route.called
        assert type(err).__name__ == _POLICY_ERROR

    @respx.mock
    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://github.com@collector.example",  # userinfo that reads like a host
            "https://github.com.collector.example",  # GitHub name as a subdomain
            "https://GitHub.com.:443",  # trailing dot: a different name, fail closed
        ],
    )
    def test_lookalike_github_hosts_get_no_token(self, endpoint: str, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
        route = respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="sha256:x"))
        with patch(_PUBLIC, return_value=False):
            _, err = _call(_fetch_digest_via_http, _http_ws(endpoint), "p", "1")
        assert not route.called
        assert type(err).__name__ == _POLICY_ERROR

    @respx.mock
    def test_catalog_backend_applies_the_same_policy(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
        route = respx.get(url__regex=r".*").mock(
            return_value=httpx.Response(200, json={"digest": "sha256:x"})
        )
        ws = FederatedWorkspace(
            id="cat",
            kind="catalog",
            endpoint="https://catalog.example/api",
            auth_mode="bearer",
            auth_secret_ref=_GITHUB_VAR,
        )
        with patch(_PUBLIC, return_value=False):
            _, err = _call(_fetch_digest_via_catalog, ws, "orders", "1")
        assert not route.called
        assert type(err).__name__ == _POLICY_ERROR

    def test_apply_gate_reports_the_refusal_without_the_value(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", _TOKEN)
        ws = _http_ws("https://registry.example", ref="AWS_SECRET_ACCESS_KEY")
        contract = {
            "consumes": [
                {"productId": "p", "upstreamWorkspace": "ext", "upstreamDigest": "sha256:a"}
            ]
        }
        with respx.mock(assert_all_called=False) as mock:
            route = mock.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="x"))
            with patch(_PUBLIC, return_value=False), caplog.at_level("WARNING"):
                rows = validate_federated_consumes(
                    contract, workspace_root=tmp_path, manifest=FederationManifest([ws])
                )
        assert not route.called
        assert len(rows) == 1 and rows[0].kind == "unreachable"
        assert "AWS_SECRET_ACCESS_KEY" in rows[0].reason
        assert _ALLOWLIST_ENV in rows[0].reason
        assert _TOKEN not in rows[0].reason
        assert _TOKEN not in caplog.text


_PORT_EVENT = "federation_secret_ref_allowlist_entry_has_port"
_BEARER = frozenset({"bearer"})
#: The one text every port warning has. Only the suggestion, ``NAME@<host>``
#: or ``NAME@`` and a hostname parsed out of the entry, varies.
_PORT_WARNING = (
    "federation_secret_ref_allowlist_entry_has_port: "
    "FLUID_FEDERATION_SECRET_REF_ALLOWLIST has an entry with a port, and it never "
    "matches, because hosts are compared without a port. The entry is not shown: "
    "copied from a URL, it can hold a password. To match, write it as {suggestion!r}, "
    "with the variable's name for NAME and the endpoint's hostname after the '@'."
)
#: Fake text standing in for a user name, password or token in an entry.
_MARK = "zq3e9amark"


def _port_warnings(caplog) -> List[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "fluid.forge.federation" and r.getMessage().startswith(_PORT_EVENT)
    ]


def _every_log_field(caplog) -> str:
    """Each record's message and every attribute (args, extras, exc_text),
    lower-cased, since a hostname is."""
    return "\n".join(repr(r.__dict__) + "\n" + r.getMessage() for r in caplog.records).lower()


class TestAllowlistEntryWithAPort:
    """Hosts are compared without their port, so ``NAME@host:port`` can never
    match. It stays fail-closed (the port does not start counting), and a
    warning says that an entry of the variable has a port and gives the form
    ``NAME@host`` that does match, instead of the operator seeing only a
    refusal that repeats the host they think they already listed.

    The warning repeats no part of the entry, not even the variable's name: an
    entry copied from a URL can hold a user name, a password or a token in any
    position, and no parse of a malformed entry can tell which part is which.
    Only the suggested host comes from the entry, and only when it is a
    hostname ``urlparse`` reads from the text after the entry's last ``@``,
    with no ``@`` in it and no ``:`` outside an IPv6 address; otherwise the
    suggestion is the placeholder ``NAME@<host>``.
    """

    @pytest.fixture(autouse=True)
    def _fresh_warnings(self, monkeypatch):
        # raising=False: the set does not exist before the fix, and the red
        # run must fail on the assertions, not on this fixture.
        monkeypatch.setattr(federation, "_ALLOWLIST_PORT_WARNED", set(), raising=False)

    @respx.mock
    def test_entry_with_a_port_stays_refused_and_the_warning_repeats_none_of_it(
        self, monkeypatch, caplog
    ):
        entry = f"{_REGISTRY_VAR}@registry.example:8443"
        monkeypatch.setenv(_REGISTRY_VAR, _TOKEN)
        monkeypatch.setenv(_ALLOWLIST_ENV, entry)
        route = respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="sha256:x"))
        ws = _http_ws("https://registry.example:8443", ref=_REGISTRY_VAR)
        with (
            patch(_PUBLIC, return_value=False),
            caplog.at_level("WARNING", logger="fluid.forge.federation"),
        ):
            _, err = _call(_fetch_digest_via_http, ws, "p", "1")
        assert not route.called, "an entry with a port must not start matching"
        assert type(err).__name__ == _POLICY_ERROR
        warnings = _port_warnings(caplog)
        assert warnings == [_PORT_WARNING.format(suggestion="NAME@registry.example")], warnings
        assert _REGISTRY_VAR not in warnings[0] and "8443" not in warnings[0]
        assert entry not in caplog.text
        assert _TOKEN not in caplog.text

        # The suggestion, with the variable's name for NAME, is what matches.
        monkeypatch.setenv(_ALLOWLIST_ENV, f"{_REGISTRY_VAR}@registry.example")
        with patch(_PUBLIC, return_value=False):
            digest, err = _call(_fetch_digest_via_http, ws, "p", "1")
        assert err is None and digest is not None
        assert route.calls.last.request.headers["authorization"] == f"Bearer {_TOKEN}"

    @pytest.mark.parametrize(
        "entry, endpoint, host",
        [
            ("REGISTRY_TOKEN@registry.example:8443", "https://registry.example:8443", None),
            ("REGISTRY_TOKEN@Registry.Example:443", "https://registry.example", "registry.example"),
            ("REGISTRY_TOKEN@10.0.0.5:8443", "https://10.0.0.5:8443", "10.0.0.5"),
            ("REGISTRY_TOKEN@[fd00::5]:8443", "https://[fd00::5]:8443", "fd00::5"),
            # The endpoint's whole netloc copied into the entry: the hostname
            # the endpoint is compared by has no userinfo either.
            (
                "REGISTRY_TOKEN@deploy@registry.example:8443",
                "https://deploy@registry.example:8443",
                "registry.example",
            ),
            # The same copy from an IPv6 netloc.
            ("REGISTRY_TOKEN@deploy@[fd00::5]:8443", "https://deploy@[fd00::5]:8443", "fd00::5"),
            ("REGISTRY_TOKEN@a@b@[FD00::5]:443", "https://[fd00::5]", "fd00::5"),
        ],
    )
    def test_the_suggested_host_is_the_one_the_endpoint_is_compared_by(
        self, entry: str, endpoint: str, host: str | None, monkeypatch, caplog
    ):
        host = host or "registry.example"
        monkeypatch.setenv(_REGISTRY_VAR, _TOKEN)
        ws = _http_ws(endpoint, ref=_REGISTRY_VAR)
        resolve = federation._resolve_federation_credential

        monkeypatch.setenv(_ALLOWLIST_ENV, f"OTHER_TOKEN, {entry}")
        with caplog.at_level("WARNING", logger="fluid.forge.federation"):
            _, err = _call(lambda: resolve(ws, url=endpoint, modes=_BEARER))
        assert type(err).__name__ == _POLICY_ERROR, "fail closed: the port entry never matches"
        assert _port_warnings(caplog) == [_PORT_WARNING.format(suggestion=f"NAME@{host}")]

        monkeypatch.setenv(_ALLOWLIST_ENV, f"{_REGISTRY_VAR}@{host}")
        assert resolve(ws, url=endpoint, modes=_BEARER) == _TOKEN

    @pytest.mark.parametrize(
        "allowlist",
        [
            "REGISTRY_TOKEN@registry.example",
            "REGISTRY_TOKEN",
            "REGISTRY_TOKEN@fd00::5",  # a bare IPv6 address has colons but no port
            "REGISTRY_TOKEN@2001:db8::443",
        ],
    )
    def test_entries_without_a_port_do_not_warn(self, allowlist: str, monkeypatch, caplog):
        monkeypatch.setenv(_ALLOWLIST_ENV, allowlist)
        with caplog.at_level("WARNING", logger="fluid.forge.federation"):
            federation._secret_ref_allowlist()
        assert not _port_warnings(caplog)

    @pytest.mark.parametrize(
        "entry, host",
        [
            # userinfo copied from a URL's netloc, before the host
            (f"REGISTRY_TOKEN@deploy:{_MARK}@registry.example:8443", "registry.example"),
            (f"REGISTRY_TOKEN@deploy:{_MARK}@[fd00::5]:8443", "fd00::5"),
            (f"REGISTRY_TOKEN@{_MARK}@registry.example:443", "registry.example"),
            (f"REGISTRY_TOKEN@a@{_MARK}@registry.example:8443", "registry.example"),
            (f"REGISTRY_TOKEN@{_MARK}%40x@registry.example:8443", "registry.example"),
            (f"REGISTRY_TOKEN@u:{_MARK}\uff20x@registry.example:8443", "registry.example"),
            (f"REGISTRY_TOKEN@ {_MARK} :pw@registry.example:8443 ", "registry.example"),
            (f"REGISTRY_TOKEN@{_MARK}\t:pw@registry.example:8443", "registry.example"),
            (
                f"REGISTRY_TOKEN@{_MARK}:pw@REGISTRY.EXAMPLE:\uff18\uff14\uff14\uff13",
                "registry.example",
            ),
            # a netloc pasted with no NAME@ before it, or another name that
            # is no environment variable name
            (f"deploy:{_MARK}@registry.example:8443", "registry.example"),
            (f"deploy:{_MARK}@[fd00::5]:8443", "fd00::5"),
            (f"deploy:{_MARK}@deploy@[fd00::5]:8443", "fd00::5"),
            (f"{_MARK}-user@registry.example:8443", "registry.example"),
            (f"fake {_MARK}@registry.example:8443", "registry.example"),
            (f"\uff2e{_MARK}@registry.example:8443", "registry.example"),
            # a token that is shaped like a variable name, used as the user name
            (f"ghp_{_MARK}0000@registry.example:443", "registry.example"),
            (f"{_MARK}@registry.example:443", "registry.example"),
            (f"{_MARK}_x@u:pw@registry.example:8443", "registry.example"),
            # userinfo inside hand-written brackets: the text after the last
            # '@' is no host urlparse can read
            (f"REGISTRY_TOKEN@[u:{_MARK}@x]:8443", None),
            (f"REGISTRY_TOKEN@[u:{_MARK}@fd00::5]:8443", None),
            (f"REGISTRY_TOKEN@[{_MARK}@fd00::5]:8443", None),
            (f"REGISTRY_TOKEN@[{_MARK}@registry.example]:8443", None),
            # brackets around no IPv6 address (Python 3.10's urlparse returns
            # their content as the hostname; 3.11+ raises ValueError)
            (f"REGISTRY_TOKEN@[u:{_MARK}]:8443", None),
            (f"REGISTRY_TOKEN@deploy:{_MARK}@[nothex]:8443", None),
        ],
    )
    def test_the_warning_repeats_no_part_of_the_entry(
        self, entry: str, host: str | None, monkeypatch, caplog
    ):
        """Whatever the entry holds, the warning is the one fixed text with a
        suggested host, which is a hostname read from after the entry's last
        ``@`` or the placeholder ``<host>``. Nothing else of the entry, its
        variable name included, reaches the log record."""
        suggestion = f"NAME@{host or '<host>'}"
        monkeypatch.setenv(_ALLOWLIST_ENV, entry)
        with caplog.at_level("WARNING", logger="fluid.forge.federation"):
            federation._secret_ref_allowlist()
        assert _port_warnings(caplog) == [_PORT_WARNING.format(suggestion=suggestion)]
        assert _MARK not in _every_log_field(caplog), caplog.text
        assert suggestion.count("@") == 1
        # Fail closed: neither the entry nor its parts match the host.
        if host:
            assert not federation._secret_ref_permitted(_REGISTRY_VAR, host)

    @pytest.mark.parametrize("delim", ["/", "?", "#"])
    @pytest.mark.parametrize(
        "host, works",
        [("registry.example:8443", "registry.example"), ("[fd00::5]:8443", "fd00::5")],
    )
    def test_userinfo_holding_a_url_delimiter_stays_out_of_the_suggestion(
        self, delim: str, host: str, works: str, monkeypatch, caplog
    ):
        """A URL's authority ends at the first ``/``, ``?`` or ``#`` (RFC 3986
        section 3.2), so parsing ``user/rest:password@host:port`` as one netloc
        gives the start of the userinfo as the hostname. The host is read from
        the text after the last ``@``, as it is for the endpoint the entry is
        compared with, and the suggestion, with the variable's name for NAME,
        is what matches."""
        entry = f"{_REGISTRY_VAR}@{_MARK}{delim}rest:pw@{host}"
        monkeypatch.setenv(_ALLOWLIST_ENV, entry)
        with caplog.at_level("WARNING", logger="fluid.forge.federation"):
            federation._secret_ref_allowlist()
        assert _port_warnings(caplog) == [_PORT_WARNING.format(suggestion=f"NAME@{works}")]
        assert _MARK not in _every_log_field(caplog), caplog.text
        assert not federation._secret_ref_permitted(_REGISTRY_VAR, works)

        monkeypatch.setenv(_ALLOWLIST_ENV, f"{_REGISTRY_VAR}@{works}")
        assert federation._secret_ref_permitted(_REGISTRY_VAR, works)

    def test_a_comma_in_a_pasted_password_repeats_none_of_it(self, monkeypatch, caplog):
        """``,`` separates entries, so ``NAME@user:pa,ss@host:port`` is read as
        ``NAME@user:pa`` (no port) and ``ss@host:port``, whose name is the end
        of the password."""
        monkeypatch.setenv(_ALLOWLIST_ENV, f"REGISTRY_TOKEN@u:pa,{_MARK}@registry.example:8443")
        with caplog.at_level("WARNING", logger="fluid.forge.federation"):
            federation._secret_ref_allowlist()
        assert _port_warnings(caplog) == [_PORT_WARNING.format(suggestion="NAME@registry.example")]
        assert _MARK not in _every_log_field(caplog), caplog.text

    def test_a_user_name_with_a_password_of_digits_reads_as_a_host_without_the_password(
        self, monkeypatch, caplog
    ):
        """``user:12345`` has the shape of ``host:port``. docs/FEDERATION.md says
        the suggestion then repeats the user name, never the password, both for
        such an entry and for a password split in two by a ``,``."""
        monkeypatch.setenv(
            _ALLOWLIST_ENV, "REGISTRY_TOKEN@fakeuser:90817,26354@registry.example:8443"
        )
        with caplog.at_level("WARNING", logger="fluid.forge.federation"):
            federation._secret_ref_allowlist()
        assert _port_warnings(caplog) == [
            _PORT_WARNING.format(suggestion="NAME@fakeuser"),
            _PORT_WARNING.format(suggestion="NAME@registry.example"),
        ]
        assert "90817" not in caplog.text and "26354" not in caplog.text

    def test_each_entry_is_warned_about_once_and_no_entry_is_kept(self, monkeypatch, caplog):
        """Warn-once is keyed on a hash of the entry, so the process keeps no
        copy of a password from an entry it has warned about."""
        first = f"REGISTRY_TOKEN@deploy:{_MARK}1@registry.example:8443"
        second = f"REGISTRY_TOKEN@deploy:{_MARK}2@registry.example:8443"
        monkeypatch.setenv(_ALLOWLIST_ENV, f"{first},{second},{first}")
        with caplog.at_level("WARNING", logger="fluid.forge.federation"):
            for _ in range(3):
                federation._secret_ref_allowlist()
        assert len(_port_warnings(caplog)) == 2, caplog.text
        warned = federation._ALLOWLIST_PORT_WARNED
        assert len(warned) == 2
        assert not any(_MARK in key or "deploy" in key for key in warned), warned


# ──────────────────── 3. credentials only over https ────────────────────


class TestTransport:
    @respx.mock
    @pytest.mark.parametrize("mode", ["bearer", "basic"])
    def test_http_endpoint_with_a_credential_is_refused(self, mode: str, monkeypatch):
        monkeypatch.setenv("REGISTRY_TOKEN", _TOKEN)
        monkeypatch.setenv(_ALLOWLIST_ENV, "REGISTRY_TOKEN@registry.example")
        route = respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="sha256:x"))
        ws = _http_ws("http://registry.example", mode=mode, ref="REGISTRY_TOKEN")
        with patch(_PUBLIC, return_value=False):
            _, err = _call(_fetch_digest_via_http, ws, "p", "1")
        assert not route.called, "a credential must not travel in cleartext"
        assert type(err).__name__ == _POLICY_ERROR
        assert "https" in str(err)

    @respx.mock
    def test_http_endpoint_without_a_credential_still_works(self):
        route = respx.get("http://registry.example/p/1/digest").mock(
            return_value=httpx.Response(200, text="sha256:anon")
        )
        ws = FederatedWorkspace(id="anon", kind="http_registry", endpoint="http://registry.example")
        with patch(_PUBLIC, return_value=False):
            assert _fetch_digest_via_http(ws, "p", "1") == "sha256:anon"
        assert "Authorization" not in route.calls[0].request.headers


# ──────────────────── 4. redirects ──────────────────────────────────────


class TestRedirectCredentialHandling:
    def _ws(self) -> FederatedWorkspace:
        return _http_ws("https://registry.example/api", ref="REGISTRY_TOKEN")

    @pytest.fixture(autouse=True)
    def _allow(self, monkeypatch):
        monkeypatch.setenv("REGISTRY_TOKEN", _TOKEN)
        monkeypatch.setenv(_ALLOWLIST_ENV, "REGISTRY_TOKEN@registry.example")

    @respx.mock
    @pytest.mark.parametrize(
        "target",
        [
            "https://mirror.example/x",  # other host
            "https://registry.example:8443/x",  # same host, other port
        ],
    )
    def test_cross_origin_hop_drops_credentials(self, target: str):
        respx.get("https://registry.example/api/p/1/digest").mock(
            return_value=httpx.Response(302, headers={"Location": target})
        )
        hop = respx.get(target).mock(return_value=httpx.Response(200, text="sha256:mirror"))
        with patch(_PUBLIC, return_value=False):
            assert _fetch_digest_via_http(self._ws(), "p", "1") == "sha256:mirror"
        sent = hop.calls[0].request.headers
        assert "Authorization" not in sent
        assert _TOKEN not in repr(sent)

    @respx.mock
    def test_credentials_stay_dropped_when_a_later_hop_comes_back(self):
        respx.get("https://registry.example/api/p/1/digest").mock(
            return_value=httpx.Response(302, headers={"Location": "https://mirror.example/x"})
        )
        respx.get("https://mirror.example/x").mock(
            return_value=httpx.Response(302, headers={"Location": "https://registry.example/back"})
        )
        back = respx.get("https://registry.example/back").mock(
            return_value=httpx.Response(200, text="sha256:back")
        )
        with patch(_PUBLIC, return_value=False):
            assert _fetch_digest_via_http(self._ws(), "p", "1") == "sha256:back"
        assert "Authorization" not in back.calls[0].request.headers

    @respx.mock
    def test_same_origin_hop_keeps_credentials(self):
        respx.get("https://registry.example/api/p/1/digest").mock(
            return_value=httpx.Response(301, headers={"Location": "/v2/p/1/digest"})
        )
        hop = respx.get("https://registry.example/v2/p/1/digest").mock(
            return_value=httpx.Response(200, text="sha256:moved")
        )
        with patch(_PUBLIC, return_value=False):
            assert _fetch_digest_via_http(self._ws(), "p", "1") == "sha256:moved"
        assert hop.calls[0].request.headers["Authorization"] == f"Bearer {_TOKEN}"

    @respx.mock
    def test_https_to_http_downgrade_is_refused(self, caplog):
        respx.get("https://registry.example/api/p/1/digest").mock(
            return_value=httpx.Response(302, headers={"Location": "http://registry.example/x"})
        )
        downgraded = respx.get("http://registry.example/x").mock(
            return_value=httpx.Response(200, text="sha256:plain")
        )
        with patch(_PUBLIC, return_value=False), caplog.at_level("WARNING"):
            assert _fetch_digest_via_http(self._ws(), "p", "1") is None
        assert not downgraded.called
        assert "downgrade" in caplog.text
        assert _TOKEN not in caplog.text

    @respx.mock
    def test_downgrade_is_refused_without_a_credential_too(self):
        respx.get("https://open.example/p/1/digest").mock(
            return_value=httpx.Response(302, headers={"Location": "http://open.example/x"})
        )
        downgraded = respx.get("http://open.example/x").mock(
            return_value=httpx.Response(200, text="sha256:plain")
        )
        ws = FederatedWorkspace(id="open", kind="http_registry", endpoint="https://open.example")
        with patch(_PUBLIC, return_value=False):
            assert _fetch_digest_via_http(ws, "p", "1") is None
        assert not downgraded.called

    def test_docstrings_no_longer_claim_redirects_are_disabled(self):
        for fn in (
            federation._fetch_digest_via_http,
            federation._fetch_digest_via_catalog,
            federation._federation_http_get,
        ):
            doc = " ".join((fn.__doc__ or "").split())
            assert "redirect-disabled" not in doc
            assert "disables redirect-following" not in doc
