# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The run-secrets file: a runner hands this process exactly the secrets its run may use.

Command Center's ``runner-env`` handoff (design section 6, decision Q3) writes
the values of one run's references to a file and starts forge with
:data:`RUN_SECRETS_ENV` pointing at it. While the variable is set:

* a reference resolves from the file and no store is ever asked, so the
  process needs no identity of its own;
* a reference the file does not list does not resolve at all, so a contract
  that names more than its runner granted fails closed.

The file is a JSON object ``{"<reference>": "<value>"}``. It must be a regular
file (not a link), owned by this user, mode 0600 or stricter, and at most
1 MiB. Keys are canonicalised like any reference (``aws://x`` and
``aws-sm://x`` are one key). Each value becomes a ``SecretStr`` and is
registered with the exact-value redactor as it is read. The runner owns the
file's lifetime; forge never writes or deletes it.

:data:`RUN_SECRETS_ENV` is protected (``credentials/protected_env.py``): only
the real environment, never a project dotenv file, can set it.
"""

from __future__ import annotations

import json
import os
import stat
from typing import Any, Dict, Optional

from fluid_build._errors import doc_url

from .errors import SecretError, SecretPolicyError, SecretResolveError
from .ref import SecretRef, parse_secret_ref
from .run import RunState

#: Operator-only. A 0600 JSON file ``{"<reference>": "<value>"}`` written by the
#: runner that started this process (Command Center's ``runner-env``).
RUN_SECRETS_ENV = "FLUID_RUN_SECRETS_FILE"  # pragma: allowlist secret

_RUN_SECRETS_MAX_BYTES = 1024 * 1024


def _run_file_error(path: str, why: str) -> SecretResolveError:
    return SecretResolveError(
        what=f"the run-secrets file {path} cannot be used",
        why=why,
        fix=(
            f"The runner that sets {RUN_SECRETS_ENV} writes it: a JSON object of "
            '{"<reference>": "<value>"}, mode 0600, owned by this user. Unset the '
            "variable to resolve from the stores instead."
        ),
        doc=doc_url("secrets"),
        extras={"reason": "run_file"},
    )


def _read_run_file(path: str) -> Dict[str, Any]:
    """Read and check the handoff file; every value becomes a registered ``SecretStr``."""
    from pydantic import SecretStr

    from fluid_build.observability.secret_redactor import register_secret

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _run_file_error(path, f"it could not be opened ({type(exc).__name__}).") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _run_file_error(path, "it is not a regular file.")
        getuid = getattr(os, "getuid", None)
        if getuid is not None:
            if info.st_uid != getuid():
                raise _run_file_error(path, "it is owned by another user.")
            if info.st_mode & 0o077:
                raise _run_file_error(
                    path,
                    f"its mode is {stat.S_IMODE(info.st_mode):04o}; group and others "
                    "must have no access (0600).",
                )
        if info.st_size > _RUN_SECRETS_MAX_BYTES:
            raise _run_file_error(path, "it is larger than 1 MiB.")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(_RUN_SECRETS_MAX_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _run_file_error(path, "it is not UTF-8 JSON.") from None
    if not isinstance(payload, dict):
        raise _run_file_error(path, "it is not a JSON object.")
    handed: Dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(value, str) or not value:
            raise _run_file_error(path, f"the entry for {key!r} is not a non-empty string.")
        try:
            canonical = parse_secret_ref(key).canonical
        except SecretError:
            raise _run_file_error(path, f"the key {key!r} is not a secret reference.") from None
        if canonical in handed and handed[canonical].get_secret_value() != value:
            raise _run_file_error(
                path, f"it lists {canonical} twice (as {key!r} too) with different values."
            )
        register_secret(value)
        handed[canonical] = SecretStr(value)
    return handed


def handed(state: RunState) -> Optional[Dict[str, Any]]:
    """The run's handoff map, read once; ``None`` when no run-secrets file is set."""
    path = os.environ.get(RUN_SECRETS_ENV, "").strip()
    if not path:
        return None
    if state.handed is not None:
        return state.handed
    handoff = _read_run_file(path)
    if state.keep_values:
        state.handed = handoff
    return handoff


def not_handed(ref: SecretRef) -> SecretPolicyError:
    return SecretPolicyError(
        what=f"{ref.canonical} was not handed to this run",
        why=(
            f"{RUN_SECRETS_ENV} is set, so this run resolves only the references its "
            "runner listed in that file, and never asks a store itself."
        ),
        fix=(
            "Add the reference to the run's secrets where the runner is configured (in "
            "Command Center, the deployment's credentials), or correct the contract."
        ),
        doc=doc_url("secrets"),
    )
