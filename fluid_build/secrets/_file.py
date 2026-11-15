# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``file:///absolute/path``: a mounted secret file.

For Docker and Kubernetes secrets and files a Vault or OpenBao Agent renders.
The policy confines the path to the operator's prefixes (``/run/secrets/`` by
default, after Kafka Connect's ``allowed.paths``); this module checks the
RESOLVED path against them again, so a symlink under an allowed prefix cannot
point the read outside it. A Kubernetes secret volume is a chain of symlinks
inside its own mount directory, which stays allowed.
"""

from __future__ import annotations

import os
import stat

from fluid_build._errors import doc_url

from .errors import SecretPolicyError, SecretResolveError
from .policy import check_prefix
from .ref import SecretRef
from .registry import Existence
from .run import RunState

#: A secret file is small; anything larger is not one.
MAX_BYTES = 64 * 1024


def _real(ref: SecretRef) -> SecretRef:
    """``ref`` with its path resolved, re-checked against the prefixes."""
    real = os.path.realpath(ref.locator)
    resolved = SecretRef(scheme="file", locator=real, written_scheme="file")
    if real != ref.locator:
        try:
            check_prefix(resolved)
        except SecretPolicyError:
            raise SecretPolicyError(
                what=f"{ref.canonical} points outside the allowed file prefixes",
                why=f"It resolves through a link to {real}, which no allowed prefix covers.",
                fix="Reference the file where it really is, under an allowed prefix "
                "(FLUID_SECRET_PREFIXES).",
                doc=doc_url("secrets"),
            ) from None
    return resolved


def _unreadable(ref: SecretRef, why: str, reason: str) -> SecretResolveError:
    return SecretResolveError(
        what=f"{ref.canonical} did not resolve",
        why=why,
        fix="Mount the secret at that path for the user running fluid, or correct the path.",
        doc=doc_url("secrets"),
        extras={"reason": reason},
    )


class FileResolver:
    """Reads one regular file, at most 64 KiB, and drops one trailing newline."""

    scheme = "file"

    def resolve(self, ref: SecretRef, run: RunState) -> str:
        real = _real(ref)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(real.locator, flags)
        except FileNotFoundError:
            raise _unreadable(ref, "no file exists at that path.", "not_found") from None
        except OSError as exc:
            raise _unreadable(
                ref, f"it could not be opened ({type(exc).__name__}).", "denied"
            ) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _unreadable(ref, "it is not a regular file.", "invalid")
            if info.st_size > MAX_BYTES:
                raise _unreadable(
                    ref, "it is larger than 64 KiB, so it is not a secret.", "invalid"
                )
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                raw = handle.read(MAX_BYTES + 1)
        finally:
            if fd >= 0:
                os.close(fd)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _unreadable(ref, "it is not UTF-8 text.", "invalid") from None
        # One trailing line break is how most tools write a secret file.
        if text.endswith("\r\n"):
            return text[:-2]
        if text.endswith("\n"):
            return text[:-1]
        return text

    def exists(self, ref: SecretRef, run: RunState) -> Existence:
        real = _real(ref)
        if not os.path.isfile(real.locator):
            return Existence(False, "no regular file at that path")
        if not os.access(real.locator, os.R_OK):
            return Existence(None, "the file exists but this user cannot read it")
        return Existence(True, "the file exists and is readable")
