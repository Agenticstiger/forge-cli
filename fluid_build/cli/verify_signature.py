# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid verify-signature`` — cosign keyless verification of a tgz bundle.

Sibling to ``fluid bundle --sign``. Checks that the bundle's Sigstore bundle,
``<bundle>.sigstore.json``, matches the tgz content and was signed by a key, or keyless by an
OIDC identity and issuer: pinned exactly, or matching regexps.

Usage::

    fluid verify-signature bundle.tgz --key cosign.pub
    fluid verify-signature bundle.tgz \\
        --certificate-identity 'https://github.com/myorg/orders/.github/workflows/ci.yml@refs/heads/main' \\
        --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'

Keyless verification names its signer, exactly or by regexp: there is no default, and an empty
value is refused, since anyone can get a Sigstore certificate for their own identity. A failed
verification returns exit 1; a missing cosign binary, Sigstore bundle or signer returns exit 2.
"""

from __future__ import annotations

import argparse
import logging
from typing import Optional

from fluid_build.cli._common import CLIError
from fluid_build.cli._signing import cosign_available, verify_bundle
from fluid_build.cli.console import cprint

COMMAND = "verify-signature"
logger = logging.getLogger(__name__)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        COMMAND,
        help="Verify a Sigstore-cosign-signed tgz bundle",
        description=(
            "Companion to ``fluid bundle --sign``. Verifies the "
            "Sigstore bundle ``<bundle>.sigstore.json`` matches the bundle "
            "bytes and was signed by --key, or keyless by the OIDC identity "
            "and issuer given (exactly, or by regexp). Sign and verify with "
            "the same cosign major version. Exit 0 = valid; exit 1 = invalid signature or "
            "identity mismatch; exit 2 = config error (bundle not found, "
            "cosign not on PATH, etc.)."
        ),
        epilog=(
            "Examples:\n"
            "  fluid verify-signature /tmp/bundle.tgz --key cosign.pub\n"
            "  fluid verify-signature /tmp/bundle.tgz \\\n"
            "      --certificate-identity 'https://github.com/myorg/orders/.github/workflows/ci.yml@refs/heads/main' \\\n"
            "      --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'\n"
            "  fluid verify-signature /tmp/bundle.tgz \\\n"
            "      --identity-regexp 'https://github.com/myorg/.*' \\\n"
            "      --oidc-issuer-regexp 'https://token.actions.githubusercontent.com'\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "bundle",
        help="Path to the tgz bundle to verify.",
    )
    p.add_argument(
        "--bundle",
        dest="sigstore_bundle",
        default=None,
        metavar="PATH",
        help="The Sigstore bundle fluid bundle --sign wrote. Default: <bundle>.sigstore.json.",
    )
    p.add_argument(
        "--key",
        default=None,
        help=(
            "Keyed-mode verification: path or KMS URI of the public key. "
            "Selects keyed verify over the default keyless Fulcio-cert "
            "verify. Matches bundles signed with ``bundle --sign "
            "--sign-key``. Supports file paths, awskms://, gcpkms://, "
            "azurekms://, hashivault://, k8s://, pkcs11://, file://."
        ),
    )
    p.add_argument(
        "--certificate-identity",
        default=None,
        metavar="ID",
        help="Keyless: the signer's exact certificate identity (with --certificate-oidc-issuer).",
    )
    p.add_argument(
        "--certificate-oidc-issuer",
        default=None,
        metavar="URL",
        help="Keyless: the signer's exact OIDC issuer (with --certificate-identity).",
    )
    p.add_argument(
        "--identity-regexp",
        default=None,
        help=(
            "Keyless: a regexp the signer's certificate identity must match, "
            "instead of --certificate-identity. Ignored in keyed mode."
        ),
    )
    p.add_argument(
        "--oidc-issuer-regexp",
        default=None,
        help=(
            "Keyless: a regexp the signer's OIDC issuer must match, instead "
            "of --certificate-oidc-issuer."
        ),
    )
    for gone in ("--signature", "--certificate"):
        # gone with the .sig/.pem files: named so the refusal says where to go
        p.add_argument(gone, dest=f"gone_{gone[2:]}", default=None, help=argparse.SUPPRESS)
    p.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Per-subprocess cosign verify-blob timeout in seconds (default 120).",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace, _logger: Optional[logging.Logger] = None) -> int:
    """Entry point wired to ``func=run`` in :func:`register`.

    The ``_logger`` parameter is accepted (but ignored) because the
    fluid CLI dispatcher uniformly calls ``args.func(args, logger)``
    on every registered subcommand. Commands that don't need the
    logger just accept it as an ignored positional.
    """
    if not cosign_available():
        raise CLIError(
            2,
            "verify_signature_cosign_missing",
            {"hint": ("install cosign from https://docs.sigstore.dev/cosign/installation/")},
        )

    key_ref = getattr(args, "key", None)
    mode = "keyed" if key_ref else "keyless"
    cprint(
        f"[verify-signature] checking {args.bundle} (mode: {mode})",
        markup=False,
    )
    for gone in ("signature", "certificate"):
        if getattr(args, f"gone_{gone}", None) is not None:
            raise CLIError(
                2,
                "verify_signature_sig_pem_gone",
                {
                    "flag": f"--{gone}",
                    "hint": (
                        "fluid bundle --sign writes one Sigstore bundle, <bundle>.sigstore.json, "
                        "and .sig/.pem files are no longer read: name another one with --bundle "
                        "PATH, and sign the bundle again if it has only .sig/.pem"
                    ),
                },
            )
    exact = (
        getattr(args, "certificate_identity", None),
        getattr(args, "certificate_oidc_issuer", None),
    )
    if any(v is not None for v in exact) and key_ref:
        raise CLIError(
            2,
            "signing_identity_not_pinned",
            {"hint": "--key verifies keyed signatures; the certificate flags are keyless"},
        )
    result = verify_bundle(
        args.bundle,
        sigstore_bundle=getattr(args, "sigstore_bundle", None),
        key_ref=key_ref,
        certificate_identity=exact[0],
        certificate_oidc_issuer=exact[1],
        identity_regexp=args.identity_regexp,
        oidc_issuer_regexp=args.oidc_issuer_regexp,
        timeout=args.timeout,
    )

    if result["exit_code"] == 0:
        cprint(
            f"[verify-signature] \u2714 signature valid: {args.bundle}",
            markup=False,
        )
        return 0

    from fluid_build.cli._signing import stderr_last_line

    cprint(
        f"[verify-signature] \u2718 verification failed "
        f"(exit {result['exit_code']}): {stderr_last_line(result)}",
        markup=False,
    )
    # cosign exit 1 = verification failure; anything else we also
    # surface as 1 to match "validation failed" semantic.
    return 1
