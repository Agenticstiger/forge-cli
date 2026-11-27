# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Usage errors around keyless signer pins: schedule-sync's verify flags need
``--verify-signature``, verify-signature's epilog examples run, and the not-pinned error
names the flags of the command that raised it. cosign is never run."""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path
from unittest.mock import patch

import pytest

from fluid_build.cli import schedule_sync, verify_signature
from fluid_build.cli._common import CLIError

_VERIFY_FLAGS = {
    "verify_key": "cosign.pub",
    "verify_certificate_identity": "ci@example.com",
    "verify_certificate_oidc_issuer": "https://issuer.example.com",
    "verify_identity_regexp": ".*",
    "verify_oidc_issuer_regexp": ".*",
}


def _sync_args(dags_dir: Path, **overrides) -> argparse.Namespace:
    values = {
        "scheduler": "airflow",
        "dags_dir": str(dags_dir),
        "destination": "s3://bucket/dags/",
        "environment_name": None,
        "location": None,
        "workspace": None,
        "env": "dev",
        "dry_run": False,
        "timeout": 60,
        "report": None,
        "bundle": None,
        "verify_signature": False,
        "git_commit_author": None,
    }
    values.update({name: None for name in _VERIFY_FLAGS})
    values.update(overrides)
    return argparse.Namespace(**values)


def _subparser(module, name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    module.register(parser.add_subparsers())
    return parser._subparsers._group_actions[0].choices[name]


@pytest.fixture()
def dags_dir(tmp_path: Path) -> Path:
    d = tmp_path / "schedule"
    (d / "prod_a").mkdir(parents=True)
    (d / "prod_a" / "dag_one.py").write_text("# stub dag\n", encoding="utf-8")
    return d


@pytest.fixture()
def signed_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "b.tgz"
    bundle.write_bytes(b"x")
    Path(str(bundle) + ".sigstore.json").write_bytes(b"{}")
    return bundle


class TestVerifyFlagsNeedTheGate:
    @pytest.mark.parametrize("flag", sorted(_VERIFY_FLAGS))
    def test_each_flag_alone_exits_2_and_pushes_nothing(self, dags_dir, flag):
        args = _sync_args(dags_dir, **{flag: _VERIFY_FLAGS[flag]})
        with (
            patch.object(schedule_sync, "_push") as push,
            patch.object(schedule_sync, "_which_or_raise", return_value="/bin/aws") as which,
            pytest.raises(CLIError) as err,
        ):
            schedule_sync.run(args)
        assert err.value.exit_code == 2
        push.assert_not_called()
        which.assert_not_called()

    def test_an_empty_string_counts_as_given(self, dags_dir):
        args = _sync_args(dags_dir, verify_identity_regexp="")
        with patch.object(schedule_sync, "_push") as push, pytest.raises(CLIError) as err:
            schedule_sync.run(args)
        assert err.value.exit_code == 2
        push.assert_not_called()

    def test_the_help_says_the_flags_need_the_gate(self):
        text = " ".join(_subparser(schedule_sync, "schedule-sync").format_help().split())
        assert "Ignored when --verify-signature is not set" not in text

    def test_with_the_gate_the_flags_still_work(self, dags_dir, signed_bundle):
        args = _sync_args(
            dags_dir,
            verify_signature=True,
            bundle=str(signed_bundle),
            dry_run=True,
            verify_certificate_identity="ci@example.com",
            verify_certificate_oidc_issuer="https://issuer.example.com",
        )
        ok = {"exit_code": 0, "key_mode": "keyless", "stderr_tail": ""}
        with (
            patch("fluid_build.cli._signing.cosign_available", return_value=True),
            patch("fluid_build.cli._signing.verify_bundle", return_value=ok) as verify,
            patch.object(schedule_sync, "_which_or_raise", return_value="/bin/aws"),
        ):
            rc = schedule_sync.run(args)
        assert rc == 0
        assert verify.call_args.kwargs["certificate_identity"] == "ci@example.com"

    def test_unsigned_run_without_any_flag_is_unchanged(self, dags_dir):
        args = _sync_args(dags_dir, dry_run=True)
        with patch.object(schedule_sync, "_which_or_raise", return_value="/bin/aws"):
            assert schedule_sync.run(args) == 0


class TestVerifySignatureEpilog:
    def _examples(self) -> list[list[str]]:
        text = (_subparser(verify_signature, "verify-signature").epilog or "").replace("\\\n", " ")
        return [
            shlex.split(line.strip())
            for line in text.splitlines()
            if line.strip().startswith("fluid verify-signature")
        ]

    def test_every_example_reaches_cosign(self, tmp_path):
        examples = self._examples()
        assert examples
        bundle = tmp_path / "bundle.tgz"
        bundle.write_bytes(b"x")
        Path(str(bundle) + ".sigstore.json").write_bytes(b"{}")
        key = tmp_path / "cosign.pub"
        key.write_bytes(b"k")
        for words in examples:
            words = [str(bundle) if w == "/tmp/bundle.tgz" else w for w in words]
            words = [str(key) if w == "cosign.pub" else w for w in words]
            parser = argparse.ArgumentParser()
            verify_signature.register(parser.add_subparsers())
            args = parser.parse_args(words[1:])
            done = {"exit_code": 0, "stdout_tail": "", "stderr_tail": ""}
            with (
                patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
                patch("fluid_build.cli._signing._run_cosign", return_value=done) as cosign,
            ):
                assert verify_signature.run(args) == 0, words
            cosign.assert_called_once()


class TestNotPinnedNamesTheCallersFlags:
    def test_verify_signature_names_its_own_flags(self, tmp_path):
        bundle = tmp_path / "bundle.tgz"
        bundle.write_bytes(b"x")
        Path(str(bundle) + ".sigstore.json").write_bytes(b"{}")
        args = argparse.Namespace(
            bundle=str(bundle),
            sigstore_bundle=None,
            certificate_identity=None,
            certificate_oidc_issuer=None,
            identity_regexp=None,
            oidc_issuer_regexp=None,
            timeout=10,
        )
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            pytest.raises(CLIError) as err,
        ):
            verify_signature.run(args)
        text = str(err.value.context)
        assert err.value.event == "signing_identity_not_pinned"
        assert "--certificate-identity" in text and "--identity-regexp" in text
        assert "--verify-" not in text

    def test_schedule_sync_names_its_own_flags(self, dags_dir, signed_bundle):
        args = _sync_args(dags_dir, verify_signature=True, bundle=str(signed_bundle))
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch("fluid_build.cli._signing.subprocess.run") as run,
            pytest.raises(CLIError) as err,
        ):
            schedule_sync.run(args)
        run.assert_not_called()
        text = str(err.value.context)
        assert err.value.event == "signing_identity_not_pinned"
        for flag in (
            "--verify-certificate-identity",
            "--verify-certificate-oidc-issuer",
            "--verify-identity-regexp",
            "--verify-oidc-issuer-regexp",
        ):
            assert flag in text
        assert "--certificate-identity" not in text.replace("--verify-certificate-identity", "")


class TestScheduleSyncEpilog:
    def test_every_example_parses_and_passes_the_gate_check(self):
        """Documented examples are run as tests (the idea behind trycmd)."""
        text = (_subparser(schedule_sync, "schedule-sync").epilog or "").replace("\\\n", " ")
        examples = [
            shlex.split(line.strip())
            for line in text.splitlines()
            if line.strip().startswith("fluid schedule-sync")
        ]
        assert examples
        from fluid_build.cli import build_parser

        for words in examples:
            args = build_parser().parse_args(words[1:])
            schedule_sync._require_gate_for_verify_flags(args)
