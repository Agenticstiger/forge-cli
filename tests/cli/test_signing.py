# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``fluid_build.cli._signing`` + the ``fluid verify-signature``
command + ``fluid bundle --sign`` integration.

All cosign subprocess invocations are mocked — tests run offline without
a real cosign binary. The tests assert:

1. ``cosign_available`` queries PATH correctly.
2. ``sign_bundle`` / ``verify_bundle`` build the right argv (no
   shell=True anywhere): one Sigstore bundle, ``<bundle>.sigstore.json``.
3. Input validation: missing bundle, wrong extension, missing Sigstore bundle.
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
        """sign-blob --yes --bundle <bundle>.sigstore.json <bundle>: the one output cosign 3.x
        writes for a blob (no --output-signature / --output-certificate)."""
        fake_completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch(
                "fluid_build.cli._signing.subprocess.run", return_value=fake_completed
            ) as mock_run,
        ):
            result = _signing.sign_bundle(str(fake_bundle))

        signed = str(fake_bundle.resolve()) + ".sigstore.json"
        argv = mock_run.call_args.args[0]
        assert argv == [
            "/bin/cosign",
            "sign-blob",
            "--yes",
            "--bundle",
            signed,
            str(fake_bundle.resolve()),
        ]
        # Assert shell=False (the critical security property)
        assert mock_run.call_args.kwargs["shell"] is False
        assert result["exit_code"] == 0
        assert result["bundle_path"] == signed
        assert result["signed_path"] == str(fake_bundle.resolve())
        assert result["key_mode"] == "keyless"

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
    @staticmethod
    def _signed(fake_bundle: Path) -> Path:
        signed = fake_bundle.parent / (fake_bundle.name + ".sigstore.json")
        signed.write_bytes(b"{}")
        return signed

    def test_builds_canonical_argv(self, fake_bundle):
        signed = self._signed(fake_bundle)
        fake_completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch(
                "fluid_build.cli._signing.subprocess.run", return_value=fake_completed
            ) as mock_run,
        ):
            result = _signing.verify_bundle(str(fake_bundle))

        argv = mock_run.call_args.args[0]
        assert argv == [
            "/bin/cosign",
            "verify-blob",
            "--bundle",
            str(signed.resolve()),
            "--certificate-identity-regexp=.*",
            "--certificate-oidc-issuer-regexp=.*",
            str(fake_bundle.resolve()),
        ]
        assert mock_run.call_args.kwargs["shell"] is False
        assert result["exit_code"] == 0 and result["key_mode"] == "keyless"
        assert result["sigstore_bundle"] == str(signed.resolve())

    def test_raises_when_the_sigstore_bundle_is_missing(self, fake_bundle):
        """The validator must raise CLIError with an actionable
        event slug — not let cosign emit a confusing 'stat' error."""
        with patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"):
            with pytest.raises(CLIError, match="signing_sigstore_bundle_missing"):
                _signing.verify_bundle(str(fake_bundle))

    def test_a_named_sigstore_bundle_is_read(self, fake_bundle, tmp_path):
        other = tmp_path / "elsewhere.json"
        other.write_bytes(b"{}")
        fake_completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch(
                "fluid_build.cli._signing.subprocess.run", return_value=fake_completed
            ) as mock_run,
        ):
            _signing.verify_bundle(str(fake_bundle), sigstore_bundle=str(other))
        assert mock_run.call_args.args[0][2:4] == ["--bundle", str(other.resolve())]

    def test_identity_regexp_flows_into_argv(self, fake_bundle):
        self._signed(fake_bundle)
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
        assert "--certificate-identity-regexp=https://github.com/acme/.*" in argv
        assert (
            "--certificate-oidc-issuer-regexp=https://token.actions.githubusercontent.com" in argv
        )

    def test_an_exact_signer_replaces_the_regexps(self, fake_bundle):
        self._signed(fake_bundle)
        fake_completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch(
                "fluid_build.cli._signing.subprocess.run", return_value=fake_completed
            ) as mock_run,
        ):
            _signing.verify_bundle(
                str(fake_bundle),
                certificate_identity="ci@example.com",
                certificate_oidc_issuer="https://issuer.example.com",
            )
        argv = mock_run.call_args.args[0]
        assert "--certificate-identity=ci@example.com" in argv
        assert "--certificate-oidc-issuer=https://issuer.example.com" in argv
        assert not any("regexp" in a for a in argv)


# -----------------------------------------------------------------------------
# verify-signature CLI command
# -----------------------------------------------------------------------------


class TestVerifySignatureCLI:
    def _args(self, bundle_path, **overrides):
        defaults = {
            "bundle": bundle_path,
            "sigstore_bundle": None,
            "certificate_identity": None,
            "certificate_oidc_issuer": None,
            "identity_regexp": ".*",
            "oidc_issuer_regexp": ".*",
            "timeout": 10,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_exit_0_on_valid_signature(self, fake_bundle):
        (fake_bundle.parent / (fake_bundle.name + ".sigstore.json")).write_bytes(b"{}")
        fake_completed = SimpleNamespace(returncode=0, stdout="verified", stderr="")
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch("fluid_build.cli._signing.subprocess.run", return_value=fake_completed),
        ):
            rc = verify_signature.run(self._args(str(fake_bundle)))
        assert rc == 0

    def test_exit_1_on_invalid_signature(self, fake_bundle):
        (fake_bundle.parent / (fake_bundle.name + ".sigstore.json")).write_bytes(b"{}")
        fake_completed = SimpleNamespace(
            returncode=1, stdout="", stderr="signature verification failed"
        )
        with (
            patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"),
            patch("fluid_build.cli._signing.subprocess.run", return_value=fake_completed),
        ):
            rc = verify_signature.run(self._args(str(fake_bundle)))
        assert rc == 1

    def test_half_an_exact_signer_is_refused(self, fake_bundle):
        (fake_bundle.parent / (fake_bundle.name + ".sigstore.json")).write_bytes(b"{}")
        with patch("fluid_build.cli._signing.shutil.which", return_value="/bin/cosign"):
            with pytest.raises(CLIError, match="signing_identity_not_pinned"):
                verify_signature.run(
                    self._args(str(fake_bundle), certificate_identity="ci@example.com")
                )

    def test_the_bundle_flag_is_on_the_parser(self):
        parser = argparse.ArgumentParser()
        verify_signature.register(parser.add_subparsers())
        args = parser.parse_args(
            ["verify-signature", "b.tgz", "--bundle", "b.json", "--certificate-identity", "x"]
        )
        assert (args.bundle, args.sigstore_bundle, args.certificate_identity) == (
            "b.tgz",
            "b.json",
            "x",
        )
        for gone in ("--signature", "--certificate"):
            with pytest.raises(SystemExit):
                parser.parse_args(["verify-signature", "b.tgz", gone, "x"])

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
    def test_keyed_argv_takes_the_key_and_one_bundle(self, fake_bundle, tmp_path):
        """Keyed mode: ``--key <ref>`` and the same one Sigstore bundle."""
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
        assert argv[key_idx + 2 : key_idx + 4] == [
            "--bundle",
            str(fake_bundle.resolve()) + ".sigstore.json",
        ]
        assert "--output-signature" not in argv and "--output-certificate" not in argv
        assert result["key_mode"] == "keyed"

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
    def test_keyed_argv_takes_the_key_and_no_certificate_flags(self, fake_bundle, tmp_path):
        (fake_bundle.parent / (fake_bundle.name + ".sigstore.json")).write_bytes(b"{}")
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
        assert argv[argv.index("--key") + 1] == str(key)
        assert not any(a.startswith("--certificate") for a in argv)
        assert "--signature" not in argv
        assert result["key_mode"] == "keyed" and result["exit_code"] == 0


def test_blob_signing_uses_one_bundle_and_pins_the_signer(tmp_path, monkeypatch) -> None:
    """sign-blob and verify-blob take --bundle, the one shape cosign 2.x and 3.x both accept;
    keyless verification pins an exact identity and issuer, each one argv element, so a
    value that starts with '-' is never read as a flag."""
    import pytest

    blob, bundle = tmp_path / "s.intoto.jsonl", tmp_path / "s.intoto.jsonl.sigstore.json"
    for path in (blob, bundle):
        path.write_bytes(b"x")
    seen = []
    monkeypatch.setattr(_signing, "_resolve_cosign", lambda: "cosign")
    monkeypatch.setattr(
        _signing, "_run_cosign", lambda argv, timeout=0: seen.append(argv) or {"exit_code": 0}
    )
    done = _signing.sign_blob(str(blob))
    assert seen[-1] == ["cosign", "sign-blob", "--yes", "--bundle", str(bundle), str(blob)]
    assert done["bundle_path"] == str(bundle)
    _signing.sign_blob(str(blob), key_ref="awskms:///alias/k")
    assert seen[-1][3:5] == ["--key", "awskms:///alias/k"]
    _signing.verify_blob(
        str(blob),
        bundle_path=str(bundle),
        certificate_identity="-x@example.com",
        certificate_oidc_issuer="https://issuer.example.com",
    )
    assert seen[-1][:4] == ["cosign", "verify-blob", "--bundle", str(bundle)]
    assert "--certificate-identity=-x@example.com" in seen[-1]
    assert "--certificate-oidc-issuer=https://issuer.example.com" in seen[-1]
    assert not any(
        a in ("--output-signature", "--output-certificate", "--signature") for a in sum(seen, [])
    )
    with pytest.raises(_signing.CLIError):
        _signing.verify_blob(str(blob), bundle_path=str(bundle))


def test_a_failure_shows_cosign_s_error_not_its_warnings() -> None:
    result = {"stderr_tail": "WARNING: Skipping tlog verification\n\nError: invalid signature\n"}
    assert _signing.stderr_last_line(result) == "Error: invalid signature"
    assert _signing.stderr_last_line({}) == ""
