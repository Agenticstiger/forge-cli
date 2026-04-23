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

"""Sigstore cosign signing for FLUID bundles.

Three operations exposed:

- :func:`sign_bundle`   — ``cosign sign-blob`` → produces
  ``<bundle>.sig`` + ``<bundle>.pem`` next to the bundle.
- :func:`verify_bundle` — ``cosign verify-blob`` → returns success/
  failure + (on success) the OIDC identity that signed.
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
    sig_out: Optional[str] = None,
    pem_out: Optional[str] = None,
    key_ref: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Dict:
    """Sign a tgz bundle with Sigstore cosign.

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

    Arguments:
      bundle_path: Path to the .tgz bundle to sign. Validated — must
        exist, be a regular file, and have a .tgz / .tar.gz extension.
      sig_out: Destination for the signature. Defaults to
        ``<bundle>.sig``.
      pem_out: Destination for the signing certificate (keyless mode
        only — ignored in keyed mode, which produces no cert).
        Defaults to ``<bundle>.pem``.
      key_ref: Optional key reference for keyed mode (see above).
        Strict whitelist applied: must be a file path OR a URI with
        scheme in {``awskms``, ``gcpkms``, ``hashivault``, ``k8s``,
        ``pkcs11``, ``file``}. Rejects anything else to prevent
        argument-injection via a crafted ``key_ref`` value.
      timeout: Per-subprocess cap (seconds). Default 120.

    Returns a dict with ``exit_code``, ``sig_path``, ``pem_path``,
    ``key_mode`` (``"keyless"`` or ``"keyed"``), plus the redacted
    argv + stdout/stderr tails for logging.

    Raises CLIError for malformed inputs (missing bundle, wrong
    extension, cosign-not-on-PATH, bad key_ref). A non-zero exit from
    cosign is returned in the dict — callers decide how to react.
    """
    cosign = _resolve_cosign()
    bundle = _validate_bundle_path(bundle_path)
    sig_path = Path(sig_out).resolve() if sig_out else bundle.with_suffix(bundle.suffix + ".sig")
    pem_path = Path(pem_out).resolve() if pem_out else bundle.with_suffix(bundle.suffix + ".pem")

    # Build argv. The shape differs between keyless and keyed modes:
    #   keyless: cosign sign-blob --yes --output-signature --output-certificate <bundle>
    #   keyed:   cosign sign-blob --key <ref> --output-signature <bundle>  (no cert)
    if key_ref is None:
        # Keyless OIDC — Fulcio ephemeral cert.
        # --yes bypasses the confirmation prompt (we're non-interactive).
        argv = [
            cosign,
            "sign-blob",
            "--yes",
            "--output-signature",
            str(sig_path),
            "--output-certificate",
            str(pem_path),
            str(bundle),
        ]
        key_mode = "keyless"
    else:
        # Keyed mode — validate key_ref against the whitelist before
        # it reaches argv. Prevents a crafted value like ``--foo bar``
        # from being parsed as a cosign flag (though cosign itself
        # would treat it as a path; defence-in-depth).
        _validate_key_ref(key_ref)
        argv = [
            cosign,
            "sign-blob",
            "--yes",
            "--key",
            key_ref,
            "--output-signature",
            str(sig_path),
            str(bundle),
        ]
        key_mode = "keyed"
    result = _run_cosign(argv, timeout=timeout)
    result["sig_path"] = str(sig_path)
    result["pem_path"] = str(pem_path) if key_ref is None else None
    result["bundle_path"] = str(bundle)
    result["key_mode"] = key_mode
    return result


def verify_bundle(
    bundle_path: str,
    *,
    sig_path: Optional[str] = None,
    pem_path: Optional[str] = None,
    key_ref: Optional[str] = None,
    identity_regexp: str = ".*",
    oidc_issuer_regexp: str = ".*",
    timeout: int = _DEFAULT_TIMEOUT,
) -> Dict:
    """Verify a signed tgz bundle via ``cosign verify-blob``.

    **Mode selection mirrors :func:`sign_bundle`:**
    - ``key_ref=None`` → keyless verify against Fulcio cert chain.
      Matches keyless-signed bundles; requires ``.pem`` file + Fulcio
      network access (or ``SIGSTORE_TRUST_REKOR_API_PUBLIC_KEY`` /
      ``COSIGN_EXPERIMENTAL=1`` in air-gapped setups).
    - ``key_ref="<path-or-kms-uri>"`` → keyed verify against a public
      key. Matches keyed-signed bundles. No ``.pem`` involved.

    Arguments:
      bundle_path: Path to the .tgz bundle.
      sig_path: Path to the .sig. Defaults to ``<bundle>.sig``.
      pem_path: Path to the .pem (keyless mode only). Defaults to
        ``<bundle>.pem``. Ignored in keyed mode.
      key_ref: Optional public-key reference for keyed verification
        (path or KMS URI). Same whitelist as :func:`sign_bundle`.
      identity_regexp: Regexp matching the acceptable OIDC subject
        (keyless mode only). Default permissive (``.*``) — tighten
        in production to pin signer identity.
      oidc_issuer_regexp: Regexp matching the acceptable OIDC issuer
        (keyless mode only). Default permissive.
      timeout: Per-subprocess cap.

    Returns a dict with ``exit_code`` (0 = signature valid), argv,
    ``key_mode`` (``"keyless"`` or ``"keyed"``), and stdout/stderr
    tails. CLIError on malformed inputs (missing bundle/sig/pem,
    cosign not on PATH, bad key_ref).
    """
    cosign = _resolve_cosign()
    bundle = _validate_bundle_path(bundle_path)
    sig = Path(sig_path).resolve() if sig_path else bundle.with_suffix(bundle.suffix + ".sig")
    if not sig.exists():
        raise CLIError(
            2,
            "signing_sig_missing",
            {"sig_path": str(sig), "hint": "did bundle --sign run?"},
        )

    if key_ref is None:
        # Keyless verification — needs .pem + identity/issuer regexps.
        pem = Path(pem_path).resolve() if pem_path else bundle.with_suffix(bundle.suffix + ".pem")
        if not pem.exists():
            raise CLIError(
                2,
                "signing_pem_missing",
                {"pem_path": str(pem), "hint": "did bundle --sign run?"},
            )
        argv = [
            cosign,
            "verify-blob",
            "--signature",
            str(sig),
            "--certificate",
            str(pem),
            "--certificate-identity-regexp",
            identity_regexp,
            "--certificate-oidc-issuer-regexp",
            oidc_issuer_regexp,
            str(bundle),
        ]
        key_mode = "keyless"
        pem_str: Optional[str] = str(pem)
    else:
        # Keyed verification — public key only, no Fulcio.
        _validate_key_ref(key_ref)
        argv = [
            cosign,
            "verify-blob",
            "--key",
            key_ref,
            "--signature",
            str(sig),
            str(bundle),
        ]
        key_mode = "keyed"
        pem_str = None

    result = _run_cosign(argv, timeout=timeout)
    result["sig_path"] = str(sig)
    result["pem_path"] = pem_str
    result["bundle_path"] = str(bundle)
    result["key_mode"] = key_mode
    return result


__all__ = [
    "cosign_available",
    "sign_bundle",
    "verify_bundle",
]
