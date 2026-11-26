# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Sigstore cosign signing for FLUID bundles.

Three operations exposed:

- :func:`sign_bundle`   — ``cosign sign-blob --bundle`` → produces one Sigstore bundle,
  ``<bundle>.sigstore.json``, next to the bundle: the signature, the certificate (keyless)
  and the transparency-log proof.
- :func:`verify_bundle` — ``cosign verify-blob --bundle`` → returns success/failure.

One ``--bundle`` file is the only output cosign 3.x writes for a blob (``--output-signature``
and ``--output-certificate`` are gone there); cosign 2.x writes its own bundle format and 3.x
the Sigstore one, so sign and verify with the same major version.
- :func:`cosign_available` — ``shutil.which("cosign")`` guard used
  by callers that want to soft-fail when the binary is absent.

All functions use ``subprocess.run(shell=False)`` with argv as a list;
no shell metacharacter expansion paths. User-controlled paths
(``bundle_path``) are resolved via :class:`pathlib.Path` so traversal
is collapsed before reaching the subprocess. Argv is passed through
:func:`fluid_build.cli.auth._sanitize_argv` before being logged — so
a future regression that adds a credential-bearing flag doesn't leak
to logs.

**Two signing modes, caller picks at sign_bundle() time:**

1. **Keyless OIDC (default)** — ephemeral Fulcio certificate via an
   OIDC identity provider. Works out-of-the-box on:
   - **GitHub Actions** (``token.actions.githubusercontent.com``)
   - **GitLab CI** (``CI_JOB_JWT_V2`` → Fulcio — GitLab-to-Sigstore
     federation is a Sigstore ``v2.0`` first-class provider)
   - **CircleCI, Buildkite** (via their OIDC tokens)
   - **GCP** (Workload Identity Federation)
   Cosign auto-detects the provider from the environment; no extra
   flag is needed. This is the recommended CNCF supply-chain posture.

2. **Keyed mode** — pass ``key_ref`` to :func:`sign_bundle` pointing
   at a cosign-format key (``cosign.key`` + ``COSIGN_PASSWORD`` env
   var, or a KMS URI like ``awskms://``, ``gcpkms://``, ``hashivault://``).
   Use this on **Bitbucket Pipelines** (no native Sigstore OIDC
   federation), air-gapped / self-hosted CI without Fulcio network
   access, or when regulatory policy mandates long-lived key material.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from fluid_build.cli._common import CLIError
from fluid_build.cli.auth import _sanitize_argv

logger = logging.getLogger(__name__)

_COSIGN_BIN_NAME = "cosign"
_DEFAULT_TIMEOUT = 120


def cosign_available() -> bool:
    """Return True when the ``cosign`` binary is reachable on PATH.

    Used by callers that want to soft-fail when the binary is absent
    (e.g. ``bundle --sign`` without cosign installed — we hard-fail
    with an install-guidance error rather than silently skipping).
    """
    return shutil.which(_COSIGN_BIN_NAME) is not None


def _resolve_cosign() -> str:
    """Return the absolute path of ``cosign`` or raise CLIError."""
    resolved = shutil.which(_COSIGN_BIN_NAME)
    if not resolved:
        raise CLIError(
            2,
            "cosign_not_on_path",
            {
                "binary": _COSIGN_BIN_NAME,
                "hint": (
                    "install cosign from https://docs.sigstore.dev/cosign/installation/ "
                    "or set COSIGN_EXPERIMENTAL=1 with a compatible build"
                ),
            },
        )
    return resolved


def _validate_bundle_path(raw: str) -> Path:
    """Resolve + sanity-check a bundle path argument.

    - Must exist.
    - Must be a regular file (not a directory, not a symlink to a
      directory).
    - Must have ``.tgz`` or ``.tar.gz`` extension (cosign doesn't care
      about extension, but this catches the ``fluid bundle foo.yaml
      --sign`` case where the user forgot ``--format tgz``).
    """
    path = Path(raw).resolve()
    if not path.exists():
        raise CLIError(2, "signing_bundle_missing", {"path": str(path)})
    if not path.is_file():
        raise CLIError(2, "signing_bundle_not_file", {"path": str(path)})
    if path.suffix not in {".tgz", ".gz"} and not str(path).endswith(".tar.gz"):
        raise CLIError(
            2,
            "signing_bundle_wrong_format",
            {
                "path": str(path),
                "hint": (
                    "signing is only meaningful for tgz bundles; "
                    "re-run with ``fluid bundle --format tgz --sign``"
                ),
            },
        )
    return path


# Whitelist of accepted URI schemes for keyed-mode ``--key`` references.
# Must match cosign's supported key-ref shapes. Unrecognised schemes
# (or paths with shell metacharacters) are rejected before reaching argv.
_COSIGN_KEY_SCHEMES = {
    "awskms",
    "gcpkms",
    "azurekms",
    "hashivault",
    "k8s",
    "pkcs11",
    "file",
}


def _validate_key_ref(raw: str) -> None:
    """Reject any ``key_ref`` that isn't a safe path or a known KMS URI.

    Two acceptable shapes:
    - Local path to a key file (no scheme). Must be an existing file
      on disk; path traversal (``..``) is rejected pre-resolve.
    - URI with scheme in :data:`_COSIGN_KEY_SCHEMES`. The scheme is
      whitelist-checked; the opaque remainder passes through to cosign
      (which does its own parsing).

    Anything else — shell metacharacters, unknown schemes, empty
    strings — raises CLIError.
    """
    if not raw or not raw.strip():
        raise CLIError(2, "signing_key_ref_empty", {"value": raw})
    # Reject shell metacharacters unconditionally. Even though argv is
    # used (not shell=True), a crafted ``key_ref`` could still break
    # assumptions in downstream tooling that re-parses the value.
    for bad in (";", "|", "&", "`", "$", "\n", "\r", " "):
        if bad in raw:
            raise CLIError(
                2,
                "signing_key_ref_unsafe_char",
                {"value": raw, "char": bad},
            )
    if "://" in raw:
        scheme = raw.split("://", 1)[0].lower()
        if scheme not in _COSIGN_KEY_SCHEMES:
            raise CLIError(
                2,
                "signing_key_ref_unknown_scheme",
                {
                    "scheme": scheme,
                    "value": raw,
                    "allowed": sorted(_COSIGN_KEY_SCHEMES),
                },
            )
        return
    # Bare path — reject traversal, require existing file.
    if ".." in Path(raw).parts:
        raise CLIError(
            2,
            "signing_key_ref_traversal",
            {"value": raw},
        )
    p = Path(raw).resolve()
    if not p.exists():
        raise CLIError(
            2,
            "signing_key_ref_file_missing",
            {"value": raw, "resolved": str(p)},
        )
    if not p.is_file():
        raise CLIError(
            2,
            "signing_key_ref_not_a_file",
            {"value": raw, "resolved": str(p)},
        )


def _run_cosign(argv: List[str], *, timeout: int = _DEFAULT_TIMEOUT) -> Dict:
    """Execute a cosign argv + return structured result.

    Never raises on non-zero exit — returns an exit-code-carrying dict
    so callers decide whether to translate to CLIError or tolerate a
    "nothing to sign" outcome.

    argv is logged via :func:`_sanitize_argv` so credential-bearing
    flags are scrubbed — defence-in-depth, even though keyless cosign
    doesn't normally have such flags.
    """
    redacted = _sanitize_argv(argv)
    logger.info("cosign_exec", extra={"argv": redacted, "timeout": timeout})
    try:
        completed = subprocess.run(  # noqa: S603 — argv list, shell=False, paths validated
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "argv": redacted,
            "exit_code": 124,
            "stdout_tail": "",
            "stderr_tail": f"timeout after {timeout}s",
        }
    except (FileNotFoundError, PermissionError) as exc:
        return {
            "argv": redacted,
            "exit_code": 127,
            "stdout_tail": "",
            "stderr_tail": f"{type(exc).__name__}: {exc}",
        }
    return {
        "argv": redacted,
        "exit_code": completed.returncode,
        "stdout_tail": (completed.stdout or "")[-4096:],
        "stderr_tail": (completed.stderr or "")[-4096:],
    }


def sign_bundle(
    bundle_path: str,
    *,
    key_ref: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Dict:
    """Sign a tgz bundle with Sigstore cosign into ``<bundle>.sigstore.json``.

    **Mode selection:**
    - ``key_ref=None`` (default) → keyless OIDC via ambient environment
      (GitHub Actions, GitLab CI, CircleCI, GCP WIF auto-detected by
      cosign itself). A Fulcio ephemeral cert is issued; no long-lived
      key material on disk. Recommended.
    - ``key_ref="<path-or-kms-uri>"`` → keyed mode. Pass a local
      ``cosign.key`` path, or a KMS URI (``awskms://``, ``gcpkms://``,
      ``hashivault://``, ``k8s://``). Requires ``COSIGN_PASSWORD`` env
      var for encrypted local keys. Use on Bitbucket Pipelines (no
      native Sigstore OIDC federation) or air-gapped environments.

    ``bundle_path`` must exist, be a regular file and have a .tgz / .tar.gz extension;
    ``key_ref`` passes the strict whitelist of :func:`_validate_key_ref`.

    Returns the :func:`sign_blob` dict: ``exit_code``, ``bundle_path`` (the Sigstore bundle
    written), ``key_mode`` (``"keyless"`` or ``"keyed"``), plus the redacted argv and
    stdout/stderr tails, and ``signed_path``, the tgz.

    Raises CLIError for malformed inputs (missing bundle, wrong
    extension, cosign-not-on-PATH, bad key_ref). A non-zero exit from
    cosign is returned in the dict — callers decide how to react.
    """
    bundle = _validate_bundle_path(bundle_path)
    result = sign_blob(str(bundle), key_ref=key_ref, timeout=timeout)
    result["signed_path"] = str(bundle)
    return result


def sign_blob(
    path: str,
    *,
    key_ref: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Dict:
    """Sign any regular file with cosign (keyless by default, keyed with ``key_ref``) into one
    Sigstore bundle, ``<file>.sigstore.json``: the signature, the certificate and the log proof.
    For a record that is not a bundle, such as a release receipt's in-toto Statement.

    ``--bundle`` is the one output both cosign 2.x and 3.x write (3.x no longer takes
    ``--output-signature``/``--output-certificate`` for a blob); 2.x writes its own bundle
    format, 3.x the Sigstore one, so sign and verify with the same major version.

    Returns the :func:`_run_cosign` dict plus ``bundle_path``.
    Raises CLIError when ``path`` is not a regular file or cosign is not on PATH.
    """
    cosign = _resolve_cosign()
    blob = Path(path).resolve()
    if not blob.is_file():
        raise CLIError(2, "signing_blob_not_file", {"path": str(blob)})
    bundle = blob.with_name(blob.name + ".sigstore.json")
    argv = [cosign, "sign-blob", "--yes"]
    if key_ref is not None:
        _validate_key_ref(key_ref)
        argv += ["--key", key_ref]
    argv += ["--bundle", str(bundle), str(blob)]
    result = _run_cosign(argv, timeout=timeout)
    result["bundle_path"] = str(bundle)
    result["key_mode"] = "keyless" if key_ref is None else "keyed"
    return result


def verify_bundle(
    bundle_path: str,
    *,
    sigstore_bundle: Optional[str] = None,
    key_ref: Optional[str] = None,
    certificate_identity: Optional[str] = None,
    certificate_oidc_issuer: Optional[str] = None,
    identity_regexp: str = ".*",
    oidc_issuer_regexp: str = ".*",
    timeout: int = _DEFAULT_TIMEOUT,
) -> Dict:
    """Verify a signed tgz bundle from its Sigstore bundle via ``cosign verify-blob --bundle``.

    **Mode selection mirrors :func:`sign_bundle`:**
    - ``key_ref=None`` → keyless: the certificate in the Sigstore bundle must chain to
      Fulcio and name the signer. ``certificate_identity`` with ``certificate_oidc_issuer``
      pins it exactly; otherwise ``identity_regexp`` and ``oidc_issuer_regexp`` match it
      (default ``.*``, permissive: tighten in production).
    - ``key_ref="<path-or-kms-uri>"`` → keyed verify against a public key.

    ``sigstore_bundle`` defaults to ``<bundle>.sigstore.json``, what :func:`sign_bundle`
    writes. Returns the :func:`_run_cosign` dict (``exit_code`` 0 = signature valid) with
    ``key_mode`` and ``sigstore_bundle``. CLIError on malformed inputs (a missing bundle or
    Sigstore bundle, cosign not on PATH, a bad key_ref).
    """
    bundle = _validate_bundle_path(bundle_path)
    signed = (
        Path(sigstore_bundle).resolve()
        if sigstore_bundle
        else bundle.with_name(bundle.name + ".sigstore.json")
    )
    if not signed.is_file():
        raise CLIError(
            2,
            "signing_sigstore_bundle_missing",
            {"sigstore_bundle": str(signed), "hint": "did bundle --sign run?"},
        )
    result = verify_blob(
        str(bundle),
        bundle_path=str(signed),
        key_ref=key_ref,
        certificate_identity=certificate_identity,
        certificate_oidc_issuer=certificate_oidc_issuer,
        identity_regexp=None if certificate_identity else identity_regexp,
        oidc_issuer_regexp=None if certificate_oidc_issuer else oidc_issuer_regexp,
        timeout=timeout,
    )
    result["sigstore_bundle"] = str(signed)
    result["bundle_path"] = str(bundle)
    result["key_mode"] = "keyless" if key_ref is None else "keyed"
    return result


def verify_blob(
    path: str,
    *,
    bundle_path: str,
    key_ref: Optional[str] = None,
    certificate_identity: Optional[str] = None,
    certificate_oidc_issuer: Optional[str] = None,
    identity_regexp: Optional[str] = None,
    oidc_issuer_regexp: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Dict:
    """Verify a file :func:`sign_blob` signed, from its bundle, with ``cosign verify-blob``:
    against ``key_ref`` (keyed), or keyless with the signer named: exactly
    (``certificate_identity`` and ``certificate_oidc_issuer``) or, where a caller asks for
    it, by regular expressions (``identity_regexp`` and ``oidc_issuer_regexp``). Keyless
    needs one of the two for both the identity and the issuer.

    Returns the :func:`_run_cosign` dict; ``exit_code`` 0 is a valid signature.
    Raises CLIError on a malformed input or no cosign.
    """
    cosign = _resolve_cosign()
    blob = Path(path).resolve()
    if not blob.is_file() or not Path(bundle_path).is_file():
        raise CLIError(2, "signing_blob_not_file", {"path": str(blob)})
    argv = [cosign, "verify-blob", "--bundle", bundle_path]
    if key_ref is not None:
        _validate_key_ref(key_ref)
        argv += ["--key", key_ref]
    else:
        identity = (
            f"--certificate-identity={certificate_identity}"
            if certificate_identity
            else (
                f"--certificate-identity-regexp={identity_regexp}"
                if identity_regexp is not None
                else None
            )
        )
        issuer = (
            f"--certificate-oidc-issuer={certificate_oidc_issuer}"
            if certificate_oidc_issuer
            else (
                f"--certificate-oidc-issuer-regexp={oidc_issuer_regexp}"
                if oidc_issuer_regexp is not None
                else None
            )
        )
        if identity is None or issuer is None:
            raise CLIError(
                2,
                "signing_identity_not_pinned",
                {"hint": "keyless verification needs the signer's identity and issuer"},
            )
        # one argv element each, so a value that starts with "-" is never read as a flag
        argv += [identity, issuer]
    argv.append(str(blob))
    return _run_cosign(argv, timeout=timeout)


def stderr_last_line(result: Dict, limit: int = 300) -> str:
    """The last line cosign wrote to stderr, cut to ``limit``: its error, after any warnings."""
    lines = [line for line in str(result.get("stderr_tail") or "").splitlines() if line.strip()]
    return lines[-1][:limit] if lines else ""


__all__ = [
    "cosign_available",
    "sign_blob",
    "verify_blob",
    "sign_bundle",
    "verify_bundle",
]
