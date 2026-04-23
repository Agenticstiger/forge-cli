# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for ``fluid_build.cli._attestation``.

The helper emits SLSA L2 in-toto v1 Statements with SLSA Provenance v1
predicates. Tests assert:

1. The emitted statement is valid in-toto v1 (``_type`` + ``subject`` +
   ``predicateType`` + ``predicate`` — schema-matching).
2. The SLSA v1 predicate structure is right (``buildDefinition`` +
   ``runDetails``).
3. CI-system detection lights up for each supported provider (GitHub,
   GitLab, CircleCI, Azure, Bitbucket, Jenkins) via the expected env
   vars — and the fallback path emits ``localhost`` when none match.
4. Git metadata integration (commit SHA + remote URL) flows into
   ``resolvedDependencies``.
5. The bundle digest is the ``subject[0].digest.sha256`` and matches
   a re-computed SHA-256 of the file.
6. CLIError on malformed inputs (missing bundle, not-a-file).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fluid_build.cli import _attestation
from fluid_build.cli._common import CLIError

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture()
def fake_bundle(tmp_path: Path) -> Path:
    b = tmp_path / "ex.fluid.bundle.tgz"
    b.write_bytes(b"this-is-definitely-a-bundle-pinky-swear\n")
    return b


@pytest.fixture()
def clear_ci_env(monkeypatch):
    """Strip every CI-provider env var so tests see a clean slate and
    the fallback branch lights up predictably. Individual tests set
    back whichever vars they want to assert on."""
    for var in [
        "GITHUB_RUN_ID",
        "GITHUB_SERVER_URL",
        "GITHUB_REPOSITORY",
        "GITHUB_STARTED_AT",
        "CI_JOB_ID",
        "CI_JOB_URL",
        "CI_SERVER_URL",
        "CIRCLE_WORKFLOW_ID",
        "CIRCLE_BUILD_NUM",
        "CIRCLE_BUILD_URL",
        "BUILD_BUILDID",
        "BUILD_BUILDURI",
        "BITBUCKET_BUILD_NUMBER",
        "BITBUCKET_GIT_HTTP_ORIGIN",
        "BUILD_ID",
        "BUILD_URL",
        "JENKINS_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch


# -----------------------------------------------------------------------------
# _sha256_file
# -----------------------------------------------------------------------------


class TestSha256File:
    def test_matches_stdlib_hashlib(self, fake_bundle):
        expected = hashlib.sha256(fake_bundle.read_bytes()).hexdigest()
        assert _attestation._sha256_file(fake_bundle) == expected

    def test_empty_file_digest(self, tmp_path):
        f = tmp_path / "empty.tgz"
        f.write_bytes(b"")
        assert _attestation._sha256_file(f) == hashlib.sha256(b"").hexdigest()


# -----------------------------------------------------------------------------
# _collect_run_metadata — CI provider detection
# -----------------------------------------------------------------------------


class TestCollectRunMetadataFallback:
    def test_localhost_builder_when_no_ci_env(self, clear_ci_env):
        """No CI env = off-platform build. ``builder.id`` signals
        ``localhost`` so verifiers know this is NOT SLSA L2 trusted."""
        meta = _attestation._collect_run_metadata()
        assert meta["builder"]["id"] == "localhost"
        assert meta["metadata"]["invocationId"].startswith("local-")
        assert "startedOn" in meta["metadata"]


class TestCollectRunMetadataGitHub:
    def test_github_actions_metadata(self, clear_ci_env):
        clear_ci_env.setenv("GITHUB_RUN_ID", "1234567")
        clear_ci_env.setenv("GITHUB_SERVER_URL", "https://github.com")
        clear_ci_env.setenv("GITHUB_REPOSITORY", "acme/fluid-product")
        meta = _attestation._collect_run_metadata()
        assert "github.com" in meta["builder"]["id"]
        assert "acme/fluid-product" in meta["builder"]["id"]
        assert meta["metadata"]["invocationId"] == (
            "https://github.com/acme/fluid-product/actions/runs/1234567"
        )

    def test_github_enterprise_server_url(self, clear_ci_env):
        """Self-hosted GHE URLs flow through — GITHUB_SERVER_URL is
        used verbatim, not hardcoded to github.com."""
        clear_ci_env.setenv("GITHUB_RUN_ID", "42")
        clear_ci_env.setenv("GITHUB_SERVER_URL", "https://github.acme.com")
        clear_ci_env.setenv("GITHUB_REPOSITORY", "team/product")
        meta = _attestation._collect_run_metadata()
        assert meta["metadata"]["invocationId"].startswith("https://github.acme.com/team/product/")


class TestCollectRunMetadataGitLab:
    def test_gitlab_ci_metadata(self, clear_ci_env):
        clear_ci_env.setenv("CI_JOB_ID", "98765")
        clear_ci_env.setenv("CI_JOB_URL", "https://gitlab.com/acme/fluid/-/jobs/98765")
        clear_ci_env.setenv("CI_SERVER_URL", "https://gitlab.com")
        meta = _attestation._collect_run_metadata()
        assert meta["builder"]["id"].startswith("https://gitlab.com")
        assert meta["metadata"]["invocationId"] == ("https://gitlab.com/acme/fluid/-/jobs/98765")

    def test_self_hosted_gitlab(self, clear_ci_env):
        """CI_SERVER_URL is used as the builder.id prefix — works for
        self-hosted GitLab instances."""
        clear_ci_env.setenv("CI_JOB_ID", "1")
        clear_ci_env.setenv("CI_SERVER_URL", "https://gitlab.internal.acme.com")
        clear_ci_env.setenv(
            "CI_JOB_URL",
            "https://gitlab.internal.acme.com/team/product/-/jobs/1",
        )
        meta = _attestation._collect_run_metadata()
        assert meta["builder"]["id"].startswith("https://gitlab.internal.acme.com")


class TestCollectRunMetadataOthers:
    def test_circleci_detected(self, clear_ci_env):
        clear_ci_env.setenv("CIRCLE_WORKFLOW_ID", "wf-abc")
        clear_ci_env.setenv("CIRCLE_BUILD_NUM", "42")
        clear_ci_env.setenv(
            "CIRCLE_BUILD_URL", "https://app.circleci.com/pipelines/gh/acme/fluid/42"
        )
        meta = _attestation._collect_run_metadata()
        assert "circleci" in meta["builder"]["id"]
        assert meta["metadata"]["invocationId"].startswith("https://app.circleci.com")

    def test_azure_devops_detected(self, clear_ci_env):
        clear_ci_env.setenv("BUILD_BUILDID", "500")
        clear_ci_env.setenv(
            "BUILD_BUILDURI",
            "vstfs:///Build/Build/500",
        )
        meta = _attestation._collect_run_metadata()
        assert "azure" in meta["builder"]["id"]
        assert "500" in meta["metadata"]["invocationId"]

    def test_bitbucket_detected(self, clear_ci_env):
        clear_ci_env.setenv("BITBUCKET_BUILD_NUMBER", "99")
        clear_ci_env.setenv(
            "BITBUCKET_GIT_HTTP_ORIGIN",
            "https://bitbucket.org/acme/fluid",
        )
        meta = _attestation._collect_run_metadata()
        assert "bitbucket" in meta["builder"]["id"]
        assert "/results/99" in meta["metadata"]["invocationId"]

    def test_jenkins_detected(self, clear_ci_env):
        clear_ci_env.setenv("BUILD_ID", "2026-04-23_01-02-03")
        clear_ci_env.setenv("JENKINS_URL", "https://jenkins.acme.com/")
        clear_ci_env.setenv("BUILD_URL", "https://jenkins.acme.com/job/fluid/42/")
        meta = _attestation._collect_run_metadata()
        assert "jenkins.acme.com" in meta["builder"]["id"]
        assert meta["metadata"]["invocationId"].endswith("/fluid/42/")

    def test_priority_order_github_over_others(self, clear_ci_env):
        """If multiple CI env vars are set (shouldn't happen but
        defensive), GitHub wins — matches the first-match semantics
        documented in ``_collect_run_metadata``."""
        clear_ci_env.setenv("GITHUB_RUN_ID", "1")
        clear_ci_env.setenv("GITHUB_REPOSITORY", "a/b")
        clear_ci_env.setenv("CI_JOB_ID", "999")  # GitLab — should NOT win
        clear_ci_env.setenv("BUILD_ID", "x")  # Jenkins — should NOT win
        meta = _attestation._collect_run_metadata()
        assert "github" in meta["builder"]["id"]


# -----------------------------------------------------------------------------
# build_attestation — full statement shape
# -----------------------------------------------------------------------------


class TestBuildAttestation:
    def test_intoto_v1_envelope(self, fake_bundle, clear_ci_env):
        stmt = _attestation.build_attestation(str(fake_bundle))
        assert stmt["_type"] == "https://in-toto.io/Statement/v1"
        assert stmt["predicateType"] == "https://slsa.dev/provenance/v1"
        assert isinstance(stmt["subject"], list)
        assert len(stmt["subject"]) == 1
        assert stmt["subject"][0]["name"] == fake_bundle.name
        assert (
            stmt["subject"][0]["digest"]["sha256"]
            == hashlib.sha256(fake_bundle.read_bytes()).hexdigest()
        )

    def test_slsa_provenance_predicate_structure(self, fake_bundle, clear_ci_env):
        stmt = _attestation.build_attestation(str(fake_bundle))
        predicate = stmt["predicate"]
        # SLSA v1 shape: buildDefinition + runDetails mandatory keys.
        assert "buildDefinition" in predicate
        assert "runDetails" in predicate
        bd = predicate["buildDefinition"]
        assert bd["buildType"] == "https://fluid-build.io/bundle-provenance/v1"
        assert "externalParameters" in bd
        assert "resolvedDependencies" in bd
        assert isinstance(bd["resolvedDependencies"], list)
        # runDetails.builder.id is required by SLSA.
        assert "builder" in predicate["runDetails"]
        assert "id" in predicate["runDetails"]["builder"]

    def test_precomputed_digest_trusted(self, fake_bundle, clear_ci_env):
        """If the caller pre-computes the digest (e.g. bundle.py
        already did it during build_bundle_tgz), the helper uses that
        value verbatim rather than re-reading the file."""
        stmt = _attestation.build_attestation(str(fake_bundle), bundle_digest="aa" * 32)
        assert stmt["subject"][0]["digest"]["sha256"] == "aa" * 32

    def test_missing_bundle_raises(self, tmp_path, clear_ci_env):
        with pytest.raises(CLIError, match="attestation_bundle_missing"):
            _attestation.build_attestation(str(tmp_path / "missing.tgz"))

    def test_directory_as_bundle_rejected(self, tmp_path, clear_ci_env):
        with pytest.raises(CLIError, match="attestation_bundle_not_file"):
            _attestation.build_attestation(str(tmp_path))

    def test_extra_external_params_merged(self, fake_bundle, clear_ci_env):
        """Caller-supplied externalParameters override the defaults
        (and merge in any extras). Lets bundle.py add CLI flag values
        to the provenance record."""
        stmt = _attestation.build_attestation(
            str(fake_bundle),
            extra_external_params={
                "format": "tgz",
                "env": "prod",
                "sign_mode": "keyless",
            },
        )
        ext = stmt["predicate"]["buildDefinition"]["externalParameters"]
        assert ext["format"] == "tgz"
        assert ext["env"] == "prod"
        assert ext["sign_mode"] == "keyless"
        # Defaults are still there.
        assert ext["bundleOut"] == str(fake_bundle.resolve())
        assert ext["bundleDigest"].startswith("sha256:")

    def test_github_metadata_flows_into_run_details(self, fake_bundle, clear_ci_env):
        clear_ci_env.setenv("GITHUB_RUN_ID", "12345")
        clear_ci_env.setenv("GITHUB_REPOSITORY", "acme/product")
        stmt = _attestation.build_attestation(str(fake_bundle))
        run = stmt["predicate"]["runDetails"]
        assert "github.com" in run["builder"]["id"]
        assert "12345" in run["metadata"]["invocationId"]


class TestGitIntegration:
    def test_git_sha_flows_into_resolved_deps(self, fake_bundle, clear_ci_env):
        """When git rev-parse returns a SHA, it lands in
        ``resolvedDependencies[0].digest.gitCommit``. Verifiers use
        this to tie the bundle back to a specific commit."""
        fake_completed = SimpleNamespace(returncode=0, stdout="abc123def456\n", stderr="")
        with patch(
            "fluid_build.cli._attestation.subprocess.run",
            return_value=fake_completed,
        ):
            stmt = _attestation.build_attestation(str(fake_bundle))
        deps = stmt["predicate"]["buildDefinition"]["resolvedDependencies"]
        assert len(deps) >= 1
        assert deps[0]["digest"]["gitCommit"] == "abc123def456"

    def test_no_git_repo_falls_back_to_empty_deps(self, fake_bundle, clear_ci_env):
        """Off-repo build (``git rev-parse`` returns non-zero) → no
        resolvedDependencies entry. Not an error; just less trust."""
        fake_completed = SimpleNamespace(returncode=128, stdout="", stderr="not a git repo")
        with patch(
            "fluid_build.cli._attestation.subprocess.run",
            return_value=fake_completed,
        ):
            stmt = _attestation.build_attestation(str(fake_bundle))
        deps = stmt["predicate"]["buildDefinition"]["resolvedDependencies"]
        assert deps == []

    def test_git_binary_missing_does_not_raise(self, fake_bundle, clear_ci_env):
        """``git`` not on PATH is not an error — plenty of bundle-
        build contexts don't have it. The attestation still emits,
        just without resolvedDependencies."""
        with patch(
            "fluid_build.cli._attestation.subprocess.run",
            side_effect=FileNotFoundError("git not found"),
        ):
            # Should not raise.
            stmt = _attestation.build_attestation(str(fake_bundle))
        assert stmt["_type"] == "https://in-toto.io/Statement/v1"


# -----------------------------------------------------------------------------
# write_attestation — disk I/O
# -----------------------------------------------------------------------------


class TestWriteAttestation:
    def test_default_path_is_bundle_plus_intoto_jsonl(self, fake_bundle, clear_ci_env):
        result = _attestation.write_attestation(str(fake_bundle))
        assert result["path"] == str(fake_bundle.resolve()) + ".intoto.jsonl"
        assert Path(result["path"]).exists()

    def test_custom_out_path_honored(self, fake_bundle, tmp_path, clear_ci_env):
        custom = tmp_path / "my-attestation.jsonl"
        result = _attestation.write_attestation(str(fake_bundle), attest_out=str(custom))
        assert result["path"] == str(custom.resolve())
        assert custom.exists()

    def test_written_content_is_single_line_json(self, fake_bundle, clear_ci_env):
        """JSONL convention: one statement per line, newline-
        terminated. Verifiers read with a streaming JSONL parser;
        multi-line JSON would break that."""
        result = _attestation.write_attestation(str(fake_bundle))
        content = Path(result["path"]).read_text()
        # Exactly one newline at the end.
        lines = content.splitlines(keepends=True)
        assert len(lines) == 1
        assert lines[0].endswith("\n")
        # And it's valid JSON.
        stmt = json.loads(content)
        assert stmt["_type"] == "https://in-toto.io/Statement/v1"

    def test_keys_are_sorted_for_determinism(self, fake_bundle, clear_ci_env):
        """JSON is emitted with sort_keys=True so the SAME run-meta
        yields byte-identical bytes every time. InvocationId + startedOn
        change per invocation by design (an invocationId MUST be unique
        per run per SLSA spec), so this test freezes them to isolate
        the JSON-serialisation-determinism property."""
        frozen_meta = {
            "builder": {"id": "localhost"},
            "metadata": {
                "invocationId": "local-fixed-uuid-for-test",
                "startedOn": "2026-04-23T10:00:00Z",
            },
        }
        with patch(
            "fluid_build.cli._attestation._collect_run_metadata",
            return_value=frozen_meta,
        ):
            r1 = _attestation.write_attestation(str(fake_bundle))
            r2 = _attestation.write_attestation(
                str(fake_bundle),
                attest_out=str(Path(r1["path"]).parent / "other.jsonl"),
            )
        c1 = Path(r1["path"]).read_text()
        c2 = Path(r2["path"]).read_text()
        assert c1 == c2, (
            "two writes with frozen run-meta should produce byte-identical "
            "output (sort_keys=True enforces canonical JSON ordering)"
        )

    def test_invocation_id_is_unique_per_run(self, fake_bundle, clear_ci_env):
        """SLSA v1 spec mandates invocationId uniqueness. Two local
        invocations MUST produce different IDs — this is the inverse
        of the determinism test above."""
        r1 = _attestation.write_attestation(str(fake_bundle))
        r2 = _attestation.write_attestation(
            str(fake_bundle),
            attest_out=str(Path(r1["path"]).parent / "other.jsonl"),
        )
        s1 = json.loads(Path(r1["path"]).read_text())
        s2 = json.loads(Path(r2["path"]).read_text())
        assert (
            s1["predicate"]["runDetails"]["metadata"]["invocationId"]
            != s2["predicate"]["runDetails"]["metadata"]["invocationId"]
        )


# -----------------------------------------------------------------------------
# Opt-in contract — --attest flag must default to OFF
# -----------------------------------------------------------------------------


class TestAttestationOptIn:
    """``--attest`` is opt-in: default False. The helper module's
    functions are ONLY called when the operator explicitly sets the
    flag; every other ``fluid bundle`` invocation leaves no attestation
    artifact on disk. These tests lock that contract in so a future
    regression can't silently start emitting attestations by default.
    """

    def test_argparse_default_is_false(self):
        """The bundle.py argparse ``--attest`` default is False. If
        someone future-maintains bundle.py and accidentally flips
        this to ``default=True``, every caller would start emitting
        unsigned attestations — not a security hole but an unwanted
        side-effect."""
        import argparse

        from fluid_build.cli import bundle as bundle_mod

        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers(dest="command")
        bundle_mod.register(sp)
        # Parse a bundle invocation WITHOUT --attest.
        ns = parser.parse_args(["bundle", "contract.fluid.yaml"])
        assert ns.attest is False, (
            "--attest must default to False. Flipping this would emit "
            "attestation files on every bundle call, changing on-disk "
            "behaviour for every existing deployment silently."
        )

    def test_argparse_accepts_attest_flag(self):
        """The flag IS available when the operator opts in."""
        import argparse

        from fluid_build.cli import bundle as bundle_mod

        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers(dest="command")
        bundle_mod.register(sp)
        ns = parser.parse_args(["bundle", "contract.fluid.yaml", "--attest"])
        assert ns.attest is True

    def test_sign_is_independent_of_attest(self):
        """--sign and --attest are independent opt-ins. Neither
        implies the other. An operator can sign without attesting
        (minimal deployment), attest without signing (visibility
        without cryptographic binding), or combine both (full
        SLSA posture)."""
        import argparse

        from fluid_build.cli import bundle as bundle_mod

        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers(dest="command")
        bundle_mod.register(sp)
        # Just --sign
        ns = parser.parse_args(["bundle", "x.fluid.yaml", "--sign"])
        assert ns.sign is True and ns.attest is False
        # Just --attest
        ns = parser.parse_args(["bundle", "x.fluid.yaml", "--attest"])
        assert ns.sign is False and ns.attest is True
        # Both
        ns = parser.parse_args(["bundle", "x.fluid.yaml", "--sign", "--attest"])
        assert ns.sign is True and ns.attest is True


# -----------------------------------------------------------------------------
# Module surface
# -----------------------------------------------------------------------------


class TestModuleSurface:
    def test_exports(self):
        assert "build_attestation" in _attestation.__all__
        assert "write_attestation" in _attestation.__all__

    def test_intoto_statement_type_is_v1(self):
        """Regression guard — the envelope URI is stable per in-toto
        v1.x and must not be changed without a coordinated spec
        bump (would invalidate every verifier's expected type)."""
        assert _attestation._INTOTO_STATEMENT_TYPE == "https://in-toto.io/Statement/v1"

    def test_slsa_predicate_type_is_v1(self):
        """Same regression guard for the SLSA Provenance v1 URI."""
        assert _attestation._SLSA_PROVENANCE_PREDICATE_TYPE == "https://slsa.dev/provenance/v1"
