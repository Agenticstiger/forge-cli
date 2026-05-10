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

"""``fluid verify-signature`` — cosign keyless verification of a tgz bundle.

Sibling to ``fluid bundle --sign``. Checks that the bundle's .sig + .pem
files match the tgz content and were signed by an OIDC identity + issuer
matching optional regexps.

Usage::

    fluid verify-signature bundle.tgz
    fluid verify-signature bundle.tgz \\
        --identity-regexp 'https://github.com/myorg/.*' \\
        --oidc-issuer-regexp 'https://token.actions.githubusercontent.com'

Default regexps are permissive (``.*``) — tighten in production to
enforce "only bundles signed by our GitHub org are acceptable." A
failed verification returns exit 1; a missing cosign binary / missing
.sig / .pem returns exit 2 (config error).
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
            "``<bundle>.sig`` + ``<bundle>.pem`` match the bundle bytes "
            "and the signing OIDC identity / issuer matches the supplied "
            "regexps. Exit 0 = valid; exit 1 = invalid signature or "
            "identity mismatch; exit 2 = config error (bundle not found, "
            "cosign not on PATH, etc.)."
        ),
        epilog=(
            "Examples:\n"
            "  fluid verify-signature /tmp/bundle.tgz\n"
            "  fluid verify-signature /tmp/bundle.tgz \\\n"
            "      --identity-regexp 'https://github.com/myorg/.*'\n"
            "  fluid verify-signature /tmp/bundle.tgz \\\n"
            "      --oidc-issuer-regexp 'https://token.actions.githubusercontent.com'\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "bundle",
        help="Path to the tgz bundle to verify.",
    )
    p.add_argument(
        "--signature",
        default=None,
        help="Path to the .sig file. Default: <bundle>.sig.",
    )
    p.add_argument(
        "--certificate",
        default=None,
        help="Path to the .pem file (keyless mode). Default: <bundle>.pem.",
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
        "--identity-regexp",
        default=".*",
        help=(
            "Regexp matching the acceptable OIDC subject (keyless mode). "
            "Default '.*' accepts any — tighten in production to pin "
            "signer identity. Ignored in keyed mode."
        ),
    )
    p.add_argument(
        "--oidc-issuer-regexp",
        default=".*",
        help=(
            "Regexp matching the acceptable OIDC issuer. Default '.*' "
            "accepts any — set to 'https://token.actions.githubusercontent.com' "
            "to pin to GitHub Actions signers only."
        ),
    )
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
    result = verify_bundle(
        args.bundle,
        sig_path=args.signature,
        pem_path=args.certificate,
        key_ref=key_ref,
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

    stderr_tail = result.get("stderr_tail", "")
    cprint(
        f"[verify-signature] \u2718 verification failed "
        f"(exit {result['exit_code']}): {stderr_tail[:500]}",
        markup=False,
    )
    # cosign exit 1 = verification failure; anything else we also
    # surface as 1 to match "validation failed" semantic.
    return 1
