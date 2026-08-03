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

"""``fluid secrets {login,verify,rotate} <secretRef>`` — credential ops.

Wraps :mod:`fluid_build.cli.ops.auth` for the source-aligned acquisition
ingestion stack. Lives under its own ``fluid secrets`` umbrella to avoid
colliding with the legacy ``fluid auth`` command (which manages cloud
provider auth, not pipeline secrets).

Backend selection: defaults to the OS keychain (``KeychainBackend``);
``FLUID_SECRETS_INMEMORY=1`` forces ``InMemoryBackend`` for tests / CI.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from dataclasses import asdict

from fluid_build.cli._errors import SecretResolutionError
from fluid_build.cli.console import cprint

COMMAND = "secrets"


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        COMMAND,
        help="🔐 Manage acquisition-pipeline secrets (login / verify / rotate)",
        description=(
            "Stores and verifies secrets used by the source-aligned acquisition "
            "runners. Defaults to the OS keychain. The legacy `fluid auth` "
            "command manages cloud provider auth — use this for pipeline secrets "
            "(database passwords, API tokens, etc.)."
        ),
    )
    sub = p.add_subparsers(dest="secrets_subcmd", required=True)

    lp = sub.add_parser("login", help="Store a secret under <secretRef>")
    lp.add_argument("ref", help="Secret reference (e.g., postgres.prod.password)")
    # SECURITY: the secret value is read ONLY from stdin (non-tty) or an
    # interactive getpass prompt — never from argv. A ``--secret <value>``
    # flag leaves the secret visible in ``ps`` / ``/proc/<pid>/cmdline`` /
    # shell history, so it is deliberately not offered.
    lp.add_argument("--expires-at", default=None, help="ISO-8601 expiry timestamp")
    _add_common(lp)
    lp.set_defaults(cmd=COMMAND, func=_do_login)

    vp = sub.add_parser("verify", help="Verify <secretRef> exists in the backend")
    vp.add_argument("ref", help="Secret reference")
    _add_common(vp)
    vp.set_defaults(cmd=COMMAND, func=_do_verify)

    rp = sub.add_parser("rotate", help="Rotate the secret stored at <secretRef>")
    rp.add_argument("ref", help="Secret reference")
    # SECURITY: replacement secret is stdin/prompt only — see the login
    # parser above for why a ``--new-secret <value>`` flag is not offered.
    rp.add_argument("--expires-at", default=None, help="ISO-8601 expiry timestamp")
    _add_common(rp)
    rp.set_defaults(cmd=COMMAND, func=_do_rotate)


# ── Helpers ────────────────────────────────────────────────────────────


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="Emit JSON")


def _backend():
    from fluid_build.cli.ops.auth import InMemoryBackend, KeychainBackend

    if os.environ.get("FLUID_SECRETS_INMEMORY", "0") == "1":
        return InMemoryBackend()
    return KeychainBackend()


def _read_secret(*, prompt: str) -> str:
    """Read a secret value from stdin (non-interactive) or an interactive
    getpass prompt. Never accepts the value from argv — a CLI flag would
    leak it via ``ps`` / ``/proc/<pid>/cmdline`` / shell history.
    """
    if not sys.stdin.isatty():
        return sys.stdin.read().rstrip("\n")
    return getpass.getpass(prompt)


def _emit(args, result) -> int:
    payload = asdict(result) if hasattr(result, "__dataclass_fields__") else dict(result)
    if getattr(args, "json", False):
        json.dump(payload, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        cprint(("✓ " if payload.get("success") else "✗ ") + str(payload))
    return 0 if payload.get("success") else 1


# ── Verb dispatchers ───────────────────────────────────────────────────


def _do_login(args, logger: logging.Logger) -> int:
    from fluid_build.cli.ops.auth import login

    secret = _read_secret(prompt=f"Secret for {args.ref}: ")
    if not secret:
        raise SecretResolutionError.for_ref(
            ref=args.ref,
            reason="No secret value provided (stdin empty / prompt cancelled)",
            fix="Pipe the secret to stdin, or run interactively to be prompted.",
        )
    result = login(
        args.ref, obtain_secret=lambda: secret, backend=_backend(), expires_at=args.expires_at
    )
    return _emit(args, result)


def _do_verify(args, logger: logging.Logger) -> int:
    from fluid_build.cli.ops.auth import verify_secret

    result = verify_secret(args.ref, backend=_backend())
    return _emit(args, result)


def _do_rotate(args, logger: logging.Logger) -> int:
    from fluid_build.cli.ops.auth import rotate

    new_secret = _read_secret(prompt=f"New secret for {args.ref}: ")
    if not new_secret:
        raise SecretResolutionError.for_ref(
            ref=args.ref,
            reason="No replacement secret provided (stdin empty / prompt cancelled)",
            fix="Pipe the new secret to stdin, or run interactively to be prompted.",
        )
    result = rotate(args.ref, new_secret=new_secret, backend=_backend(), expires_at=args.expires_at)
    return _emit(args, result)
