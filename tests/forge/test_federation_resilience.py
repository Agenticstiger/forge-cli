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

"""Federation against a real git repo, and what happens when it is not there.

``tests/forge/test_federation_backends.py`` pins the backends with the
git layer mocked. That leaves the thing most likely to break unproven:
whether the real ``git clone`` / ``git fetch`` argv this module builds
actually works against an actual repository, and whether the digest it
computes survives a peer reformatting the same contract.

So the git tests here build a genuine repo on disk and drive real git
through it. Only ``_build_auth_url`` is patched -- to point at the local
fixture instead of a network remote -- because the endpoint allow-list
deliberately refuses ``file://`` (an SSRF/local-file-read guard), and
weakening it for a test would be testing a configuration the product
cannot produce.

The second half pins the failure posture: an upstream we cannot reach is
a *row*, not an exception, and never a reason to stop checking the other
upstreams.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fluid_build.forge.core.plan_digest import compute_contract_digest
from fluid_build.forge.federation import (
    FederatedWorkspace,
    FederationManifest,
    _fetch_digest_via_git,
    validate_federated_consumes,
)

pytestmark = pytest.mark.unit

#: Resolve fixtures from the repo, not the process CWD -- these tests read
#: source and schema files, and pytest is not always invoked from the root
#: (an IDE runner, or `pytest <abs path>` from elsewhere, both break a bare
#: relative path).
REPO_ROOT = Path(__file__).resolve().parents[2]


UPSTREAM_CONTRACT = {
    "fluidVersion": "0.7.6",
    "kind": "DataProduct",
    "id": "telco.orders",
    "name": "Telco Orders",
    "domain": "sales",
    "metadata": {"layer": "Bronze", "owner": {"team": "telco", "email": "t@example.com"}},
    "exposes": [
        {
            "exposeId": "orders",
            "kind": "table",
            "version": "1.0.0",
            "binding": {
                "platform": "local",
                "format": "parquet",
                "location": {"database": "bronze", "table": "orders"},
            },
            "contract": {"schema": [{"name": "id", "type": "integer", "required": True}]},
        }
    ],
}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        },
    )


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    """A real git repo holding a real upstream contract."""
    repo = tmp_path / "upstream.git"
    (repo / "telco.orders").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "telco.orders" / "contract.fluid.yaml").write_text(
        yaml.safe_dump(UPSTREAM_CONTRACT, sort_keys=True), encoding="utf-8"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "seed upstream contract", cwd=repo)
    return repo


@pytest.fixture
def local_cache(tmp_path: Path, monkeypatch) -> Path:
    """Redirect the federation git cache out of the developer's ~/.cache."""
    home_cache = tmp_path / "home_cache"
    monkeypatch.setattr(
        "os.path.expanduser",
        lambda p: str(home_cache) if "~" in p else p,
    )
    return home_cache


class TestAgainstARealGitRepo:
    """Real ``git clone`` against a real repository -- no git mocking."""

    def _ws(self) -> FederatedWorkspace:
        return FederatedWorkspace(
            id="telco",
            kind="git_registry",
            endpoint="https://git.example.invalid/telco",
        )

    def test_real_clone_reads_contract_and_computes_canonical_digest(
        self, upstream_repo: Path, local_cache: Path
    ):
        """End-to-end: real clone, real file read, canonical digest.

        The whole ``git clone --depth 1 -- <url> <dir>`` argv runs for
        real here. A mocked test cannot tell you that argv is well-formed.
        """
        with patch(
            "fluid_build.forge.federation._build_auth_url",
            return_value=str(upstream_repo),
        ):
            digest = _fetch_digest_via_git(self._ws(), "telco.orders", "1")

        assert digest == compute_contract_digest(UPSTREAM_CONTRACT)
        # The clone really landed on disk.
        assert (local_cache / "telco" / "telco.orders" / "contract.fluid.yaml").is_file()

    def test_upstream_reformatting_does_not_read_as_drift(
        self, upstream_repo: Path, local_cache: Path
    ):
        """A peer reformatting their contract must NOT look like drift.

        This is the property that makes federation usable at all: key
        order, indentation and quoting are formatting, not data. If the
        digest were a text hash, every upstream ``yamlfmt`` run would
        page the downstream team. Exercised across a real second commit
        and the real ``git fetch`` + ``reset --hard`` refresh path.
        """
        ws = self._ws()
        with patch(
            "fluid_build.forge.federation._build_auth_url",
            return_value=str(upstream_repo),
        ):
            first = _fetch_digest_via_git(ws, "telco.orders", "1")

            # Same data, deliberately hostile formatting: reversed key
            # order, 4-space indent, quoted scalars, a trailing comment.
            reformatted = yaml.safe_dump(UPSTREAM_CONTRACT, sort_keys=False, indent=4)
            reformatted = "# regenerated by a peer's formatter\n" + reformatted
            (upstream_repo / "telco.orders" / "contract.fluid.yaml").write_text(
                reformatted, encoding="utf-8"
            )
            _git("commit", "-aqm", "reformat only", cwd=upstream_repo)

            second = _fetch_digest_via_git(ws, "telco.orders", "1")

        assert first == second, "formatting-only change must not register as drift"

    def test_real_content_change_is_detected(self, upstream_repo: Path, local_cache: Path):
        """The converse: a real schema change MUST change the digest."""
        ws = self._ws()
        with patch(
            "fluid_build.forge.federation._build_auth_url",
            return_value=str(upstream_repo),
        ):
            before = _fetch_digest_via_git(ws, "telco.orders", "1")

            changed = json.loads(json.dumps(UPSTREAM_CONTRACT))
            changed["exposes"][0]["contract"]["schema"].append(
                {"name": "customer_id", "type": "string", "required": False}
            )
            (upstream_repo / "telco.orders" / "contract.fluid.yaml").write_text(
                yaml.safe_dump(changed, sort_keys=True), encoding="utf-8"
            )
            _git("commit", "-aqm", "add a column", cwd=upstream_repo)

            after = _fetch_digest_via_git(ws, "telco.orders", "1")

        assert before != after, "a real column addition must register as drift"


class TestUnreachableUpstreamIsARowNotAnAbort:
    """A dead registry must not be able to hide drift somewhere else."""

    @staticmethod
    def _manifest() -> FederationManifest:
        return FederationManifest(
            workspaces=[
                FederatedWorkspace(
                    id="up_a", kind="http_registry", endpoint="https://a.example.invalid"
                ),
                FederatedWorkspace(
                    id="up_b", kind="http_registry", endpoint="https://b.example.invalid"
                ),
            ]
        )

    @staticmethod
    def _contract() -> dict:
        return {
            "consumes": [
                {
                    "productId": "a.orders",
                    "exposeId": "orders",
                    "upstreamWorkspace": "up_a",
                    "upstreamDigest": "sha256:" + "a" * 64,
                },
                {
                    "productId": "b.orders",
                    "exposeId": "orders",
                    "upstreamWorkspace": "up_b",
                    "upstreamDigest": "sha256:" + "b" * 64,
                },
            ]
        }

    def test_a_real_outage_is_labelled_unreachable_not_not_wired(self, tmp_path: Path):
        """Drive the REAL fetch path, stubbing only the HTTP transport.

        This is the shape a live outage takes: all three backends return
        None on a timeout / 404 / TLS error / auth reject / failed clone,
        and `fetch_federated_digest` turns that None into an exception.
        That exception used to be a bare NotImplementedError -- the same
        one raised for a workspace kind with no fetcher at all -- so a
        partner mesh being DOWN was reported to operators as "fetcher for
        kind='http_registry' not yet wired": a claim about missing FLUID
        code, sending them to read our source instead of checking their
        own registry.

        Patching `fetch_federated_digest` itself (as an earlier version of
        this test did) hides exactly that bug, because it injects an
        exception the production code never raises. So: stub the
        transport, not the fetcher.
        """
        manifest = FederationManifest(
            workspaces=[
                FederatedWorkspace(
                    id="partner", kind="http_registry", endpoint="https://partner.example.invalid"
                )
            ]
        )
        contract = {
            "consumes": [
                {
                    "productId": "p.orders",
                    "exposeId": "orders",
                    "upstreamWorkspace": "partner",
                    "upstreamDigest": "sha256:" + "a" * 64,
                }
            ]
        }

        with patch("fluid_build.forge.federation._federation_http_get", return_value=None):
            violations = validate_federated_consumes(
                contract, workspace_root=tmp_path, manifest=manifest
            )

        assert len(violations) == 1
        assert violations[0].kind == "unreachable", (
            "a down registry must read as unreachable, not as unimplemented "
            f"FLUID code; got kind={violations[0].kind!r}"
        )
        assert "NOT checked" in violations[0].reason
        assert "not yet wired" not in violations[0].reason

    def test_an_unrecognised_workspace_kind_is_still_not_wired(self, tmp_path: Path):
        """The converse: a kind with genuinely no fetcher must keep saying
        so, or the new label would swallow a real gap in our own code."""
        manifest = FederationManifest(
            workspaces=[
                FederatedWorkspace(
                    id="odd", kind="carrier_pigeon", endpoint="https://p.example.invalid"
                )
            ]
        )
        contract = {
            "consumes": [
                {
                    "productId": "p.orders",
                    "exposeId": "orders",
                    "upstreamWorkspace": "odd",
                    "upstreamDigest": "sha256:" + "a" * 64,
                }
            ]
        }
        violations = validate_federated_consumes(
            contract, workspace_root=tmp_path, manifest=manifest
        )
        assert len(violations) == 1
        assert violations[0].kind == "not-wired"

    def test_the_unreachable_reason_does_not_echo_the_exception_body(self, tmp_path: Path):
        """Only the exception CLASS goes into the reason and the log.

        This module deliberately never logs git error bodies, because in
        HTTPS mode they echo the clone URL with the manifest's auth token
        embedded. The violation reason is operator-facing text that lands
        in CI logs, so it follows the same rule.
        """
        manifest = FederationManifest(
            workspaces=[
                FederatedWorkspace(
                    id="partner", kind="http_registry", endpoint="https://partner.example.invalid"
                )
            ]
        )
        contract = {
            "consumes": [
                {
                    "productId": "p.orders",
                    "exposeId": "orders",
                    "upstreamWorkspace": "partner",
                    "upstreamDigest": "sha256:" + "a" * 64,
                }
            ]
        }
        secret = "ghp_supersecrettoken"
        with patch(
            "fluid_build.forge.federation._federation_http_get",
            side_effect=RuntimeError(f"failed cloning https://{secret}@host/repo"),
        ):
            violations = validate_federated_consumes(
                contract, workspace_root=tmp_path, manifest=manifest
            )

        assert len(violations) == 1
        assert secret not in violations[0].reason

    def test_one_unreachable_upstream_does_not_mask_drift_in_another(self, tmp_path: Path):
        """The regression this guards.

        The fetch call sat outside any per-row handler, so the first
        failure propagated out of the whole walk -- discarding violations
        already collected for other upstreams and handing the caller a
        bare exception instead. One team's registry going down would hide
        a genuine drifted pin somewhere else entirely.
        """

        def _fake(workspace, product_id, version="1", **kwargs):
            if workspace.id == "up_a":
                raise OSError("connection refused")
            return "sha256:" + "d" * 64  # != the pinned b*64 -> real drift

        with patch("fluid_build.forge.federation.fetch_federated_digest", side_effect=_fake):
            violations = validate_federated_consumes(
                self._contract(), workspace_root=tmp_path, manifest=self._manifest()
            )

        by_kind = {v.kind: v for v in violations}
        assert set(by_kind) == {"unreachable", "drift"}, (
            "the drifted upstream must still be reported when a different "
            f"upstream is unreachable; got {[v.kind for v in violations]}"
        )
        assert by_kind["unreachable"].upstream_workspace_id == "up_a"
        assert by_kind["drift"].upstream_workspace_id == "up_b"
        assert by_kind["drift"].actual_digest == "sha256:" + "d" * 64

    def test_a_contract_with_no_federated_consumes_is_silent(self, tmp_path: Path):
        """The normal case stays quiet -- federation is opt-in."""
        violations = validate_federated_consumes(
            {"consumes": [{"productId": "local.x", "exposeId": "x"}]},
            workspace_root=tmp_path,
            manifest=FederationManifest(),
        )
        assert violations == []


class TestFetchTimeoutIsBounded:
    """``fluid apply`` reaches another mesh; it must not be able to hang there."""

    def test_git_timeout_defaults_to_thirty_seconds(self, monkeypatch):
        """Pin the default with the env var explicitly cleared.

        Without the delenv this asserts against whatever the developer
        (or CI job) happens to export, so it fails for anyone who has
        actually used the knob it documents.
        """
        from fluid_build.forge.federation import _federation_git_timeout

        monkeypatch.delenv("FLUID_FEDERATION_TIMEOUT_SECONDS", raising=False)
        assert _federation_git_timeout() == 30.0

    def test_git_timeout_is_env_overridable(self, monkeypatch):
        """Operators with a genuinely large upstream repo need headroom;
        everyone else needs the apply to give up quickly.

        Read per call, so this needs no ``importlib.reload`` -- which
        would swap the module's exception classes out from under every
        other test that imported them, and did exactly that here before
        the timeout moved out of module scope.
        """
        from fluid_build.forge.federation import _federation_git_timeout

        monkeypatch.setenv("FLUID_FEDERATION_TIMEOUT_SECONDS", "7")
        assert _federation_git_timeout() == 7.0

    @pytest.mark.parametrize(
        "bad", ["", "abc", "0", "-5", "nan-ish", "inf", "nan", "-inf", "1e400"]
    )
    def test_a_bad_override_falls_back_to_the_default(self, monkeypatch, bad: str):
        """A typo in the env var must not disable the bound it configures.

        ``FLUID_FEDERATION_TIMEOUT_SECONDS=""`` reaching
        ``subprocess.run(timeout=...)`` as a float() crash, or ``0``
        reaching it as "no timeout", would both turn a guard rail into
        the hang it exists to prevent.

        ``inf`` and ``nan`` are the subtle pair: ``float()`` accepts both,
        and a bare ``value <= 0`` check rejects neither (nan compares
        False against everything, inf is positive). They then defeat the
        bound in opposite directions -- ``timeout=inf`` never fires, and
        ``timeout=nan`` makes subprocess raise before git starts.
        """
        from fluid_build.forge.federation import _federation_git_timeout

        monkeypatch.setenv("FLUID_FEDERATION_TIMEOUT_SECONDS", bad)
        assert _federation_git_timeout() == 30.0

    def test_every_git_subprocess_call_passes_the_timeout(self):
        """No un-timed ``subprocess.run`` may exist on the fetch path --
        a single un-capped call reintroduces the hang this bounds.

        Parsed with ``ast`` rather than counted as substrings: the
        module's own docstring says ``subprocess.run(["git", "clone",
        ...])`` in prose, and a substring count reads that as a fourth
        un-timed call. Counting real call nodes is both correct and
        stricter -- it also catches a literal ``timeout=5`` that a
        string search for the constant would miss.
        """
        import ast

        src = (REPO_ROOT / "fluid_build" / "forge" / "federation.py").read_text(encoding="utf-8")
        untimed = []
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "run"):
                continue
            if not (isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
                continue
            kwargs = {k.arg for k in node.keywords}
            if "timeout" not in kwargs:
                untimed.append(node.lineno)

        assert not untimed, (
            "subprocess.run on the federation fetch path with no timeout at "
            f"line(s) {untimed} -- an unresponsive upstream would hang the apply"
        )

    def test_the_gitpython_path_is_bounded_too(self):
        """gitpython is tried BEFORE the shell-out, so bounding only the
        fallback leaves the path that actually runs (wherever gitpython
        is installed) unbounded -- while `fluid doctor` advertises the
        env var as the cap. gitpython spells it ``kill_after_timeout``.
        """
        import ast

        src = (REPO_ROOT / "fluid_build" / "forge" / "federation.py").read_text(encoding="utf-8")
        untimed = []
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute):
                continue
            if fn.attr not in {"clone_from", "fetch"}:
                continue
            if "kill_after_timeout" not in {k.arg for k in node.keywords}:
                untimed.append((fn.attr, node.lineno))

        assert not untimed, (
            f"unbounded gitpython call(s) {untimed} -- gitpython is tried first, "
            "so this is the path a hang would actually take"
        )


class TestSchemaPinsTheFederatedFields:
    """0.7.6 models the federated consume fields, and pins them together."""

    @staticmethod
    def _schema() -> dict:
        return json.loads(
            (REPO_ROOT / "fluid_build" / "schemas" / "fluid-schema-0.7.6.json").read_text(
                encoding="utf-8"
            )
        )

    def test_dependent_required_is_enforceable_at_this_draft(self):
        """``dependentRequired`` is a 2019-09+ keyword. Under Draft 7 it
        is not an error -- it is silently ignored, which would make the
        pin below look enforced while accepting an unpinned upstream."""
        assert self._schema()["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    def test_upstream_workspace_requires_a_digest(self):
        cr = self._schema()["$defs"]["consumeRef"]
        assert cr["dependentRequired"]["upstreamWorkspace"] == ["upstreamDigest"]

    def test_digest_must_be_a_sha256_pin(self):
        cr = self._schema()["$defs"]["consumeRef"]
        assert cr["properties"]["upstreamDigest"]["pattern"] == "^sha256:[0-9a-f]{64}$"
