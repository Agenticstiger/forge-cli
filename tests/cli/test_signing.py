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

"""Tests for ``fluid_build.cli._signing`` + the ``fluid verify-signature``
command + ``fluid bundle --sign`` integration.

All cosign subprocess invocations are mocked — tests run offline without
a real cosign binary. The tests assert:

1. ``cosign_available`` queries PATH correctly.
2. ``sign_bundle`` / ``verify_bundle`` build the right argv (no
   shell=True anywhere) and honour user-supplied paths.
3. Input validation: missing bundle, wrong extension, missing sig/pem.
4. The ``verify-signature`` CLI command surfaces exit codes correctly.
5. The ``bundle --sign`` path hard-fails when cosign is missing.

No real network calls, no real keys, no OIDC handshake.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fluid_build.cli import _signing, verify_signature
from fluid_build.cli._common import CLIError

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture()
def fake_bundle(tmp_path: Path) -> Path:
    """Create a placeholder tgz file on disk. No signing actually
    happens; cosign is mocked and reads the bytes as a blob."""
    b = tmp_path / "test.fluid.bundle.tgz"
    b.write_bytes(b"not-a-real-tgz-but-cosign-is-mocked\n")
    return b


# -----------------------------------------------------------------------------
# cosign_available + _resolve_cosign
# -----------------------------------------------------------------------------


class TestCosignAvailable:
    def test_true_when_which_finds_cosign(self):
        with patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"):
            assert _signing.cosign_available() is True

    def test_false_when_which_returns_none(self):
        with patch("fluid_build.cli._signing.shutil.which", return_value=None):
            assert _signing.cosign_available() is False

    def test_resolve_raises_actionable_error_when_missing(self):
        with patch("fluid_build.cli._signing.shutil.which", return_value=None):
            with pytest.raises(CLIError, match="cosign_not_on_path"):
                _signing._resolve_cosign()


# -----------------------------------------------------------------------------
# _validate_bundle_path
# -----------------------------------------------------------------------------


class TestValidateBundlePath:
    def test_accepts_existing_tgz(self, fake_bundle):
        p = _signing._validate_bundle_path(str(fake_bundle))
        assert p == fake_bundle.resolve()

    def test_rejects_missing_file(self, tmp_path):
        with pytest.raises(CLIError, match="signing_bundle_missing"):
            _signing._validate_bundle_path(str(tmp_path / "missing.tgz"))

    def test_rejects_directory(self, tmp_path):
        d = tmp_path / "somedir"
        d.mkdir()
        with pytest.raises(CLIError, match="signing_bundle_not_file"):
            _signing._validate_bundle_path(str(d))

    def test_rejects_wrong_extension(self, tmp_path):
        """A yaml bundle got through without --format tgz — the error
        explicitly tells the user what to do, not a mystery cosign
        error downstream."""
        y = tmp_path / "contract.yaml"
        y.write_text("not a tgz")
        with pytest.raises(CLIError, match="signing_bundle_wrong_format"):
            _signing._validate_bundle_path(str(y))

    def test_accepts_tar_gz_extension(self, tmp_path):
        """tar.gz is the same format as tgz — accept both spellings."""
        t = tmp_path / "bundle.tar.gz"
        t.write_bytes(b"x")
        p = _signing._validate_bundle_path(str(t))
        assert p == t.resolve()


# -----------------------------------------------------------------------------
# sign_bundle
# -----------------------------------------------------------------------------


class TestSignBundle:
    def test_builds_canonical_argv(self, fake_bundle):
        """The argv passed to subprocess.run must be exactly what
        cosign expects: sign-blob --yes --output-signature <sig>
        --output-certificate <pem> <bundle>."""
        fake_completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch(
                "fluid_build.cli._signing.subprocess.run", return_value=fake_completed
            ) as mock_run,
        ):
            result = _signing.sign_bundle(str(fake_bundle))

        argv = mock_run.call_args.args[0]
        assert argv[0] == "/bin/cosign"
        assert argv[1:4] == ["sign-blob", "--yes", "--output-signature"]
        # argv[4] is the sig path
        assert argv[4].endswith(".sig")
        assert argv[5] == "--output-certificate"
        assert argv[6].endswith(".pem")
        assert argv[7] == str(fake_bundle.resolve())

        # Assert shell=False (the critical security property)
        assert mock_run.call_args.kwargs["shell"] is False

        assert result["exit_code"] == 0
        assert result["sig_path"].endswith(".sig")
        assert result["pem_path"].endswith(".pem")

    def test_default_sig_pem_paths(self, fake_bundle):
        """Default sig/pem go next to the bundle: <bundle>.sig / .pem."""
        fake_completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch("fluid_build.cli._signing.subprocess.run", return_value=fake_completed),
        ):
            result = _signing.sign_bundle(str(fake_bundle))

        assert result["sig_path"] == str(fake_bundle.resolve()) + ".sig"
        assert result["pem_path"] == str(fake_bundle.resolve()) + ".pem"

    def test_custom_sig_pem_paths_honored(self, fake_bundle, tmp_path):
        custom_sig = tmp_path / "my.sig"
        custom_pem = tmp_path / "my.pem"
        fake_completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch("fluid_build.cli._signing.subprocess.run", return_value=fake_completed),
        ):
            result = _signing.sign_bundle(
                str(fake_bundle),
                sig_out=str(custom_sig),
                pem_out=str(custom_pem),
            )
        assert result["sig_path"] == str(custom_sig.resolve())
        assert result["pem_path"] == str(custom_pem.resolve())

    def test_non_zero_exit_returned_not_raised(self, fake_bundle):
        """cosign returning exit 1 (e.g. OIDC token issue) must be
        surfaced as a dict — callers decide whether it's fatal, not
        the helper."""
        fake_completed = SimpleNamespace(returncode=1, stdout="", stderr="OIDC token rejected")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch("fluid_build.cli._signing.subprocess.run", return_value=fake_completed),
        ):
            result = _signing.sign_bundle(str(fake_bundle))
        assert result["exit_code"] == 1
        assert "OIDC token rejected" in result["stderr_tail"]

    def test_timeout_returns_124(self, fake_bundle):
        import subprocess as sp

        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch(
                "fluid_build.cli._signing.subprocess.run",
                side_effect=sp.TimeoutExpired(cmd=["x"], timeout=1),
            ),
        ):
            result = _signing.sign_bundle(str(fake_bundle), timeout=1)
        assert result["exit_code"] == 124
        assert "timeout" in result["stderr_tail"]


# -----------------------------------------------------------------------------
# verify_bundle
# -----------------------------------------------------------------------------


class TestVerifyBundle:
    def test_builds_canonical_argv(self, fake_bundle):
        # Create the sig + pem so the validator doesn't error.
        sig = fake_bundle.parent / (fake_bundle.name + ".sig")
        pem = fake_bundle.parent / (fake_bundle.name + ".pem")
        sig.write_bytes(b"fake-sig")
        pem.write_bytes(b"fake-pem")

        fake_completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch(
                "fluid_build.cli._signing.subprocess.run", return_value=fake_completed
            ) as mock_run,
        ):
            result = _signing.verify_bundle(str(fake_bundle))

        argv = mock_run.call_args.args[0]
        assert argv[0] == "/bin/cosign"
        assert argv[1] == "verify-blob"
        assert "--signature" in argv
        assert "--certificate" in argv
        assert "--certificate-identity-regexp" in argv
        assert "--certificate-oidc-issuer-regexp" in argv
        assert argv[-1] == str(fake_bundle.resolve())
        assert mock_run.call_args.kwargs["shell"] is False
        assert result["exit_code"] == 0

    def test_raises_when_sig_missing(self, fake_bundle):
        """The validator must raise CLIError with an actionable
        event slug — not let cosign emit a confusing 'stat' error."""
        with patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"):
            with pytest.raises(CLIError, match="signing_sig_missing"):
                _signing.verify_bundle(str(fake_bundle))

    def test_raises_when_pem_missing(self, fake_bundle):
        # Create the sig but not the pem.
        sig = fake_bundle.parent / (fake_bundle.name + ".sig")
        sig.write_bytes(b"fake-sig")
        with patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"):
            with pytest.raises(CLIError, match="signing_pem_missing"):
                _signing.verify_bundle(str(fake_bundle))

    def test_identity_regexp_flows_into_argv(self, fake_bundle):
        sig = fake_bundle.parent / (fake_bundle.name + ".sig")
        pem = fake_bundle.parent / (fake_bundle.name + ".pem")
        sig.write_bytes(b"x")
        pem.write_bytes(b"x")
        fake_completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch(
                "fluid_build.cli._signing.subprocess.run", return_value=fake_completed
            ) as mock_run,
        ):
            _signing.verify_bundle(
                str(fake_bundle),
                identity_regexp="https://github.com/acme/.*",
                oidc_issuer_regexp="https://token.actions.githubusercontent.com",
            )
        argv = mock_run.call_args.args[0]
        idx = argv.index("--certificate-identity-regexp")
        assert argv[idx + 1] == "https://github.com/acme/.*"
        idx = argv.index("--certificate-oidc-issuer-regexp")
        assert argv[idx + 1] == "https://token.actions.githubusercontent.com"


# -----------------------------------------------------------------------------
# verify-signature CLI command
# -----------------------------------------------------------------------------


class TestVerifySignatureCLI:
    def _args(self, bundle_path, **overrides):
        defaults = {
            "bundle": bundle_path,
            "signature": None,
            "certificate": None,
            "identity_regexp": ".*",
            "oidc_issuer_regexp": ".*",
            "timeout": 10,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_exit_0_on_valid_signature(self, fake_bundle):
        sig = fake_bundle.parent / (fake_bundle.name + ".sig")
        pem = fake_bundle.parent / (fake_bundle.name + ".pem")
        sig.write_bytes(b"x")
        pem.write_bytes(b"x")
        fake_completed = SimpleNamespace(returncode=0, stdout="verified", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch("fluid_build.cli._signing.subprocess.run", return_value=fake_completed),
        ):
            rc = verify_signature.run(self._args(str(fake_bundle)))
        assert rc == 0

    def test_exit_1_on_invalid_signature(self, fake_bundle):
        sig = fake_bundle.parent / (fake_bundle.name + ".sig")
        pem = fake_bundle.parent / (fake_bundle.name + ".pem")
        sig.write_bytes(b"x")
        pem.write_bytes(b"x")
        fake_completed = SimpleNamespace(
            returncode=1, stdout="", stderr="signature verification failed"
        )
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch("fluid_build.cli._signing.subprocess.run", return_value=fake_completed),
        ):
            rc = verify_signature.run(self._args(str(fake_bundle)))
        assert rc == 1

    def test_cosign_missing_raises_cli_error(self, fake_bundle):
        with patch("fluid_build.cli._signing.shutil.which", return_value=None):
            with pytest.raises(CLIError, match="verify_signature_cosign_missing"):
                verify_signature.run(self._args(str(fake_bundle)))


# -----------------------------------------------------------------------------
# Argparse registration
# -----------------------------------------------------------------------------


class TestArgparseRegistration:
    def test_register_adds_subcommand(self):
        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers(dest="command")
        verify_signature.register(sp)
        ns = parser.parse_args(
            [
                "verify-signature",
                "/tmp/bundle.tgz",
                "--identity-regexp",
                "https://github.com/org/.*",
            ]
        )
        assert ns.command == "verify-signature"
        assert ns.bundle == "/tmp/bundle.tgz"
        assert ns.identity_regexp == "https://github.com/org/.*"


# -----------------------------------------------------------------------------
# Keyed-mode signing/verification — for Bitbucket + air-gapped setups
# -----------------------------------------------------------------------------


class TestKeyRefValidation:
    """``_validate_key_ref`` is the gate between a user-supplied
    ``--sign-key`` value and the cosign argv. If anything unsafe slips
    through, it lands as a cosign flag instead of a key path. These
    tests lock down the allowlist."""

    @pytest.mark.parametrize(
        "uri",
        [
            "awskms:///alias/my-key",
            "gcpkms://projects/p/locations/us/keyRings/r/cryptoKeys/k",
            "azurekms://vault.vault.azure.net/keys/k/v",
            "hashivault://my-transit-key",
            "k8s://namespace/secret-name",
        ],
    )
    def test_accepts_known_kms_schemes(self, uri):
        # Should not raise.
        _signing._validate_key_ref(uri)

    def test_accepts_existing_file_path(self, tmp_path):
        key = tmp_path / "cosign.key"
        key.write_bytes(b"fake-key-bytes")
        _signing._validate_key_ref(str(key))

    def test_rejects_unknown_scheme(self):
        with pytest.raises(CLIError, match="signing_key_ref_unknown_scheme"):
            _signing._validate_key_ref("mystery://foo/bar")

    def test_rejects_shell_metacharacters(self):
        """Even though argv uses shell=False, a crafted key_ref with
        shell metacharacters is still suspicious — fail loud."""
        for bad in ["/key.pem;rm -rf /", "/key.pem|cat", "$(whoami)/key.pem"]:
            with pytest.raises(CLIError, match="signing_key_ref_unsafe_char"):
                _signing._validate_key_ref(bad)

    def test_rejects_empty(self):
        with pytest.raises(CLIError, match="signing_key_ref_empty"):
            _signing._validate_key_ref("")
        with pytest.raises(CLIError, match="signing_key_ref_empty"):
            _signing._validate_key_ref("   ")

    def test_rejects_missing_file(self, tmp_path):
        with pytest.raises(CLIError, match="signing_key_ref_file_missing"):
            _signing._validate_key_ref(str(tmp_path / "absent.key"))

    def test_rejects_directory(self, tmp_path):
        with pytest.raises(CLIError, match="signing_key_ref_not_a_file"):
            _signing._validate_key_ref(str(tmp_path))

    def test_rejects_path_traversal(self):
        with pytest.raises(CLIError, match="signing_key_ref_traversal"):
            _signing._validate_key_ref("../../etc/shadow")


class TestSignBundleKeyedMode:
    def test_keyed_argv_omits_certificate_output(self, fake_bundle, tmp_path):
        """Keyed mode doesn't produce a Fulcio cert — argv must omit
        ``--output-certificate`` and use ``--key <ref>`` instead."""
        key = tmp_path / "cosign.key"
        key.write_bytes(b"fake-key")
        fake_completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch(
                "fluid_build.cli._signing.subprocess.run", return_value=fake_completed
            ) as mock_run,
        ):
            result = _signing.sign_bundle(str(fake_bundle), key_ref=str(key))

        argv = mock_run.call_args.args[0]
        assert argv[0] == "/bin/cosign"
        assert argv[1:3] == ["sign-blob", "--yes"]
        assert "--key" in argv
        key_idx = argv.index("--key")
        assert argv[key_idx + 1] == str(key)
        # Critical: no certificate output in keyed mode.
        assert "--output-certificate" not in argv
        assert result["key_mode"] == "keyed"
        assert result["pem_path"] is None  # No cert in keyed mode

    def test_keyed_with_kms_uri(self, fake_bundle):
        """awskms:// URIs flow through unchanged — the whitelist
        accepts them and cosign handles the KMS handshake internally."""
        fake_completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch(
                "fluid_build.cli._signing.subprocess.run", return_value=fake_completed
            ) as mock_run,
        ):
            _signing.sign_bundle(str(fake_bundle), key_ref="awskms:///alias/prod-signing")
        argv = mock_run.call_args.args[0]
        assert "awskms:///alias/prod-signing" in argv


class TestVerifyBundleKeyedMode:
    def test_keyed_argv_omits_certificate_flags(self, fake_bundle, tmp_path):
        sig = fake_bundle.parent / (fake_bundle.name + ".sig")
        sig.write_bytes(b"fake-sig")
        key = tmp_path / "pub.key"
        key.write_bytes(b"fake-pub")
        fake_completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch(
                "fluid_build.cli._signing.subprocess.run", return_value=fake_completed
            ) as mock_run,
        ):
            result = _signing.verify_bundle(str(fake_bundle), key_ref=str(key))
        argv = mock_run.call_args.args[0]
        assert "--key" in argv
        assert "--certificate" not in argv
        assert "--certificate-identity-regexp" not in argv
        assert "--certificate-oidc-issuer-regexp" not in argv
        assert result["key_mode"] == "keyed"
        assert result["pem_path"] is None

    def test_keyed_does_not_require_pem_file(self, fake_bundle, tmp_path):
        """Keyed verification only needs .sig + key_ref — no .pem
        file. The missing-pem precondition check must NOT fire in
        keyed mode (it's keyless-only)."""
        sig = fake_bundle.parent / (fake_bundle.name + ".sig")
        sig.write_bytes(b"x")
        key = tmp_path / "pub.key"
        key.write_bytes(b"x")
        # Deliberately DO NOT create the .pem file.
        fake_completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch("fluid_build.cli._signing.subprocess.run", return_value=fake_completed),
        ):
            # Should NOT raise signing_pem_missing.
            result = _signing.verify_bundle(str(fake_bundle), key_ref=str(key))
        assert result["exit_code"] == 0
