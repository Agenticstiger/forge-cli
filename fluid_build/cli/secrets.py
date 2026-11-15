# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid secrets`` — pipeline secrets: check a contract's references, and keychain ops.

``fluid secrets check <contract>`` lints every ``secretRef`` a contract names
against the grammar and the operator's policy (scheme, locator prefix, and
every host each value would be sent to), the same check ``fluid validate``
runs. ``--resolve`` also proves each reference exists in its store (KV v2
metadata, Secrets Manager ``DescribeSecret``, the file or variable being
there) without reading a single value.

``login`` / ``verify`` / ``rotate`` store, check and rotate an OS keychain
entry by name (keyring service ``fluid-forge``), through
:mod:`fluid_build.cli.ops.auth`. No ``secretRef`` scheme reads the keychain:
a contract names its secrets in a store the grammar covers. Backend: the OS
keychain (``KeychainBackend``); ``FLUID_SECRETS_INMEMORY=1`` forces
``InMemoryBackend`` for tests / CI. They live under ``fluid secrets`` to avoid
colliding with ``fluid auth``, which manages cloud provider auth.
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
from fluid_build.cli.console import cprint, cprint_json, cprint_literal

COMMAND = "secrets"


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        COMMAND,
        help="🔐 Check a contract's secret references; manage keychain secrets",
        description=(
            "check: lint a contract's secretRefs against the operator's policy "
            "(--resolve proves each exists, reading no value). login / verify / "
            "rotate: store, check and rotate an OS keychain entry by name (no "
            "secretRef scheme reads the keychain). `fluid auth` manages cloud "
            "provider auth."
        ),
    )
    sub = p.add_subparsers(dest="secrets_subcmd", required=True)

    cp = sub.add_parser(
        "check",
        help="Lint a contract's secretRefs against the operator's policy",
        description=(
            "Parses every secretRef in the contract and checks it against the operator's "
            "policy: the scheme is enabled (FLUID_SECRET_SCHEMES), the locator is under an "
            "allowed prefix (FLUID_SECRET_PREFIXES), and every host the value would be sent "
            "to is allowed (FLUID_ACQUISITION_SECRET_REF_ALLOWLIST). No value is read. Exit "
            "1 when any reference is refused (or, with --resolve, missing)."
        ),
    )
    cp.add_argument("contract", help="Path to the contract (contract.fluid.yaml)")
    cp.add_argument("--env", default=None, help="Overlay environment to apply first")
    cp.add_argument(
        "--resolve",
        action="store_true",
        help=(
            "Also prove each reference exists in its store, without reading its value "
            "(KV v2 metadata, Secrets Manager DescribeSecret). Uses this shell's store login"
        ),
    )
    _add_common(cp)
    cp.set_defaults(cmd=COMMAND, func=_do_check)

    lp = sub.add_parser("login", help="Store a keychain secret under <name>")
    lp.add_argument("ref", metavar="name", help="Keychain entry name, e.g. postgres.prod.password")
    # SECURITY: the secret value is read ONLY from stdin (non-tty) or an
    # interactive getpass prompt — never from argv. A ``--secret <value>``
    # flag leaves the secret visible in ``ps`` / ``/proc/<pid>/cmdline`` /
    # shell history, so it is deliberately not offered.
    lp.add_argument("--expires-at", default=None, help="ISO-8601 expiry timestamp")
    _add_common(lp)
    lp.set_defaults(cmd=COMMAND, func=_do_login)

    vp = sub.add_parser("verify", help="Verify a keychain secret <name> exists")
    vp.add_argument("ref", metavar="name", help="Keychain entry name")
    _add_common(vp)
    vp.set_defaults(cmd=COMMAND, func=_do_verify)

    rp = sub.add_parser("rotate", help="Rotate the keychain secret stored under <name>")
    rp.add_argument("ref", metavar="name", help="Keychain entry name")
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


def _check_rows(contract: dict, resolve: bool) -> list:
    """One row per reference: where, what, verdict; with ``resolve``, existence too."""
    from fluid_build.secrets import SecretError, secret_exists, secret_run
    from fluid_build.secrets.contract import lint_secret_refs
    from fluid_build.secrets.ref import shown

    rows = []
    # A run of its own, so a token --resolve obtains (a JWT login) is revoked
    # at the end. "secrets-check" is not a stage that may fetch values.
    with secret_run("secrets-check"):
        for finding in lint_secret_refs(contract):
            raw = finding.site.raw
            row = {
                "pointer": finding.site.pointer,
                # A malformed value may be a secret pasted in place of its
                # reference: never print it back.
                "reference": (
                    finding.ref.canonical
                    if finding.ref is not None
                    else (shown(raw) if raw is not None else None)
                ),
                "scheme": finding.ref.scheme if finding.ref else None,
                "ok": finding.ok,
            }
            if finding.warning is not None:
                row["warning"] = f"{finding.warning.what}. {finding.warning.fix}"
            if finding.error is not None:
                row.update(
                    code=finding.error.code,
                    what=finding.error.what,
                    why=finding.error.why,
                    fix=finding.error.fix,
                )
            elif resolve and finding.ref is not None:
                try:
                    existence = secret_exists(finding.ref)
                    row.update(exists=existence.found, detail=existence.detail)
                except SecretError as exc:
                    row.update(exists=None, detail=f"{exc.why} {exc.fix}".strip())
                if row.get("exists") is not True:
                    row["ok"] = False
            rows.append(row)
    return rows


def _do_check(args, logger: logging.Logger) -> int:
    from fluid_build.cli._common import load_contract_with_overlay

    contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
    rows = _check_rows(contract if isinstance(contract, dict) else {}, bool(args.resolve))
    ok = all(row["ok"] for row in rows)
    if getattr(args, "json", False):
        cprint_json(json.dumps({"contract": args.contract, "ok": ok, "references": rows}, indent=2))
        return 0 if ok else 1
    if not rows:
        cprint_literal(f"No secret references in {args.contract}.")
        return 0
    for row in rows:
        mark = "✅" if row["ok"] else "❌"
        reference = f": {row['reference']}" if row["reference"] else ""
        cprint_literal(f"{mark} {row['pointer']}{reference}")
        if "what" in row:
            cprint_literal(f"   {row['what']}")
            cprint_literal(f"   why: {row['why']}")
            cprint_literal(f"   fix: {row['fix']}")
        elif "exists" in row:
            verdict = {True: "exists", False: "missing", None: "cannot tell"}[row["exists"]]
            cprint_literal(f"   {verdict}: {row['detail']}")
        if "warning" in row:
            cprint_literal(f"   ⚠️  {row['warning']}")
    refused = sum(1 for row in rows if not row["ok"])
    if refused:
        cprint(f"{refused} of {len(rows)} secret references need attention.")
    else:
        cprint(f"All {len(rows)} secret references pass.")
    return 0 if ok else 1


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
